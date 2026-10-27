"""Segmentation settings, per-image analysis, and exports.

Ported from Etaluma_Live_Segmentation_Optimized.py, the standalone script the
lab validated its masks with. The settings below are that script's, value for
value; change them only after comparing masks/ROIs against a baseline run.

Two things differ from the script, neither in its results:

  * ROI tracing and the minimum-size filter work on each label's bounding box
    instead of comparing the whole frame once per label. On a 1900x1900 frame
    with ~1,600 cells the whole-frame version spent ~10 s per image after the
    GPU had finished. The output is identical (see selftest.test_analysis).
  * Outputs go to <output_dir>/<capture folder>/..., derived from the input
    path, so two captures with identical file names can never collide.

Output layout per capture folder, as the script wrote it:
  masks/<stem>_masks.tiff   uint16/uint32 label image, 0 = background
  rois/<stem>.zip           ImageJ ROI set
  debug_plots/<stem>.png    outlines over the percentile-stretched image
  density_per_well.csv      one row per image
"""
from __future__ import annotations

import colorsys
import csv
import os
import threading
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Settings:
    # Cellpose eval
    diameter: float = 45
    flow_threshold: float = 0.5
    cellprob_threshold: float = -0.2
    min_size: int = 150
    niter: int = 500
    resample: bool = True
    # Cellpose's batch_size: 256x256 network patches per GPU pass. NOT the
    # number of images; that is the worker's batch size.
    cellpose_batch_size: int = 48
    # Post-filter and physical scale (3.38 mm = 1800 px on this microscope)
    min_equiv_diameter_um: float = 26
    pixel_size_um: float = 3380.0 / 1800.0
    # Preview stretch only; the model normalises on its own.
    norm_percentile: tuple[float, float] = (2, 99.8)
    save_debug_plots: bool = True
    save_rois: bool = True
    save_mask_tiffs: bool = True

    def eval_kwargs(self) -> dict[str, Any]:
        return dict(
            diameter=self.diameter, flow_threshold=self.flow_threshold,
            cellprob_threshold=self.cellprob_threshold, min_size=self.min_size,
            niter=self.niter, normalize=True, do_3D=False,
            batch_size=self.cellpose_batch_size, resample=self.resample,
        )

    def describe(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT = Settings()

CSV_FIELDS = ["well", "image_name", "cells", "density_cells_per_cm2", "area_um2"]


def to_gray(arr: np.ndarray) -> np.ndarray:
    """The 2D plane the model sees: as-is, or a max over channels."""
    if arr.ndim == 2:
        return arr
    if arr.ndim == 3:
        return np.max(arr, axis=-1)
    raise ValueError(f"unexpected TIFF shape: {arr.shape}")


def segment(model, grays: list[np.ndarray], s: Settings = DEFAULT) -> list[np.ndarray]:
    """Segment equal-sized 2D images in one GPU call, every image independent.

    One image goes in as a plain 2D array, exactly as the script did. Several
    go in as one N,Y,X,1 array: a Python list would make Cellpose loop over
    them one at a time and nothing would be batched.
    """
    if not grays:
        return []
    opts = s.eval_kwargs()
    if len(grays) == 1:
        x = grays[0]
    else:
        if len({g.shape for g in grays}) != 1:
            raise ValueError("a GPU batch must contain equally sized 2D images")
        x = np.stack([g[..., np.newaxis] for g in grays])
        opts["channel_axis"] = -1
    masks, _flows, _styles = model.eval(x, **opts)
    if len(grays) == 1:
        out = [np.asarray(masks)]
    else:
        stacked = np.asarray(masks)
        if stacked.ndim != 3 or stacked.shape[0] != len(grays):
            raise ValueError(f"expected {len(grays)} masks, received {stacked.shape}")
        out = [stacked[i] for i in range(len(grays))]
    for g, m in zip(grays, out):
        if m.shape != g.shape:
            raise ValueError(f"mask shape {m.shape} != image shape {g.shape}")
    return [m.astype(np.int32, copy=False) for m in out]


def _padded_box(bbox: tuple[int, int, int, int], shape: tuple[int, int]) -> tuple[slice, slice]:
    """The label's bounding box grown by one pixel, clipped to the frame.

    Clipped rather than zero-padded: a cell touching the frame edge must keep
    the open contour find_contours gives it on the full frame.
    """
    r0, c0, r1, c1 = bbox
    return (slice(max(0, r0 - 1), min(shape[0], r1 + 1)),
            slice(max(0, c0 - 1), min(shape[1], c1 + 1)))


def filter_min_diameter(labels: np.ndarray, s: Settings = DEFAULT) -> np.ndarray:
    from skimage.measure import regionprops
    from skimage.segmentation import relabel_sequential

    if s.min_equiv_diameter_um <= 0 or labels.size == 0 or labels.max() == 0:
        return labels
    min_d_px = float(s.min_equiv_diameter_um) / float(s.pixel_size_um)
    out = labels.copy()
    for p in regionprops(labels):
        if np.sqrt(4.0 * float(p.area) / np.pi) < min_d_px:
            sl = p.slice
            region = out[sl]
            region[region == p.label] = 0
    out, _, _ = relabel_sequential(out)
    return out


def rois_and_polygons(labels: np.ndarray, min_points: int = 6):
    from roifile import ImagejRoi
    from skimage.measure import find_contours, regionprops

    rois, polys = [], []
    h, w = labels.shape
    for p in sorted(regionprops(labels), key=lambda p: p.label):
        if int(p.area) < 5:
            continue
        rs, cs = _padded_box(p.bbox, labels.shape)
        mask = (labels[rs, cs] == p.label).astype(np.uint8)
        contours = find_contours(mask, level=0.5)
        if not contours:
            continue
        c = max(contours, key=lambda a: a.shape[0])
        if c.shape[0] < min_points:
            continue
        xs = np.clip(np.rint(c[:, 1] + cs.start), 0, w - 1).astype(np.int16)
        ys = np.clip(np.rint(c[:, 0] + rs.start), 0, h - 1).astype(np.int16)
        pts = np.column_stack([xs, ys])
        roi = ImagejRoi.frompoints(pts)
        roi.name = f"cell_{int(p.label):05d}"
        rois.append(roi)
        polys.append(pts.astype(np.int32))
    return rois, polys


@dataclass
class Result:
    labels: np.ndarray
    cells: int
    density: float
    area_um2: float
    rois: list = field(default_factory=list)
    polys: list = field(default_factory=list)


def analyze(labels: np.ndarray, s: Settings = DEFAULT) -> Result:
    """Counts come from the filtered label image, not from ROI export."""
    labels = filter_min_diameter(labels, s)
    cells = int(labels.max()) if labels.size else 0
    rois, polys = (rois_and_polygons(labels) if (s.save_rois or s.save_debug_plots)
                   else ([], []))
    h, w = labels.shape[:2]
    area_um2 = float(h * w) * (s.pixel_size_um ** 2)
    area_cm2 = area_um2 / 1e8
    density = float(cells / area_cm2) if area_cm2 > 0 else float("nan")
    return Result(labels=labels, cells=cells, density=density, area_um2=area_um2,
                  rois=rois, polys=polys)


# --- exports ---------------------------------------------------------------

def _to_uint8(x: np.ndarray, pct: tuple[float, float]) -> np.ndarray:
    x = x.astype(np.float32, copy=False)
    lo, hi = np.percentile(x, pct)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.zeros_like(x, dtype=np.uint8)
    return (np.clip((x - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)


def save_mask_tiff(path: Path, labels: np.ndarray) -> None:
    from tifffile import imwrite

    if labels.ndim != 2 or np.any(labels < 0):
        raise ValueError("expected a nonnegative 2D label mask")
    max_label = int(labels.max()) if labels.size else 0
    if max_label > np.iinfo(np.uint32).max:
        raise ValueError(f"mask label {max_label} does not fit in uint32")
    dtype = np.uint16 if max_label <= np.iinfo(np.uint16).max else np.uint32
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.partial.tiff")
    try:
        imwrite(tmp, labels.astype(dtype, copy=False), photometric="minisblack")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_rois_zip(path: Path, rois: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for roi in rois:
            zf.writestr(f"{roi.name}.roi", roi.tobytes())


def save_outline_png(path: Path, image: np.ndarray, polys: list, s: Settings = DEFAULT) -> None:
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    overlay = Image.fromarray(_to_uint8(to_gray(image), s.norm_percentile)).convert("RGB")
    draw = ImageDraw.Draw(overlay)
    # Golden-angle hue spacing keeps neighbouring cells distinct. Preview only.
    for i, pts in enumerate(polys):
        if pts.shape[0] < 2:
            continue
        rgb = colorsys.hsv_to_rgb((0.03 + i * 0.618033988749895) % 1.0, 0.93, 1.0)
        coords = [(int(x), int(y)) for x, y in pts]
        draw.line(coords + [coords[0]], fill=tuple(round(v * 255) for v in rgb), width=2)
    overlay.save(path, format="PNG", compress_level=1)


_csv_lock = threading.Lock()


def export(src: Path, image: np.ndarray, res: Result, out_dir: Path,
           s: Settings = DEFAULT) -> Path:
    """Write every per-image output into out_dir; return the mask path.

    The CSV row is written last, so a row only exists for an image whose
    files all landed.
    """
    stem = src.stem
    mask_path = out_dir / "masks" / f"{stem}_masks.tiff"
    if s.save_debug_plots:
        save_outline_png(out_dir / "debug_plots" / f"{stem}.png", image, res.polys, s)
    if s.save_rois:
        save_rois_zip(out_dir / "rois" / f"{stem}.zip", res.rois)
    if s.save_mask_tiffs:
        save_mask_tiff(mask_path, res.labels)
    csv_path = out_dir / "density_per_well.csv"
    with _csv_lock:
        new = not csv_path.exists() or csv_path.stat().st_size == 0
        with open(csv_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if new:
                w.writeheader()
            w.writerow({"well": stem, "image_name": src.name, "cells": res.cells,
                        "density_cells_per_cm2": res.density, "area_um2": res.area_um2})
    return mask_path
