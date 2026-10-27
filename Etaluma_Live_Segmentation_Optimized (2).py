"""Live Etaluma TIFF segmentation with Cellpose CPDINO.

Run: python3 -u Etaluma_Live_Segmentation_Optimized.py
Stop: Ctrl+C (safe to restart; completed image names are read from the CSV).

Coverage: every Green/PC TIFF and every Z-plane is processed independently.
Image-level GPU micro-batching is used when equal-sized TIFFs are ready.
The microscope can keep acquiring while this script runs, but real-time
catch-up depends on GPU throughput relative to the incoming image rate.

Before each new acquisition, verify INPUT_FOLDER. The output folder is
automatically named after the capture folder to prevent cross-run skips.
Each image exports a labeled mask TIFF, an ImageJ ROI ZIP, and an outline PNG.
For scientific comparisons, validate ROI masks/counts against a baseline run.
"""

from __future__ import annotations

import os
import csv
import colorsys
import zipfile
import time
import gc
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple, Dict, List, Any, Optional

import numpy as np
from PIL import Image, ImageDraw
from tifffile import imwrite as write_tiff
from natsort import natsorted

from cellpose import models, core, io as cpio

from roifile import ImagejRoi
from skimage.measure import find_contours, regionprops
from skimage.segmentation import relabel_sequential

# ============================================================
# INPUT / OUTPUT
# ============================================================
INPUT_FOLDER = Path("/home/neil-linlab/Projects/Etaluma_API/LumaViewPro-4.0.0-beta/capture/ProtocolData/20261027_155550")
# For each new capture, change only INPUT_FOLDER. Results are separated by run.
OUTPUT_FOLDER = Path("/home/neil-linlab/Desktop/RP/Mock Test Output")

TILE_GLOB = "*.tiff"

PLATE_ROWS = "ABCDEFGH"
PLATE_COLS = list(range(1, 13))

SAVE_DEBUG_PLOTS = True  # PNG with visible cell outlines; costs some save time.
SAVE_ROIS = True
SAVE_MASK_TIFFS = True  # Lossless labeled masks matching CSV counts / ROI ZIPs.

# A batch means separate 2D TIFF images; Cellpose's BATCH_SIZE below means
# internal network patches and is NOT the number of TIFF images per batch.
IMAGES_PER_GPU_BATCH = 2
POLL_INTERVAL_SECONDS = 0.5
FILE_STABLE_SECONDS = 3.0  # Checked concurrently for all TIFFs, not per file.
RETRY_DELAY_SECONDS = 5.0

# ============================================================
# PIXEL SIZE (µm per pixel)
# Given: 3.38 mm corresponds to 1800 pixels
# 3.38 mm = 3380 µm
# ============================================================
PIXEL_SIZE_UM = 3380.0 / 1800.0  # 1.877777...

# ============================================================
# CELLPOSE CONFIG (keep close to your original)
# ============================================================
GPU = "0"
FLOW_THRESHOLD = 0.5
CELLPROB_THRESHOLD = -0.2
DIAMETER = 45
MIN_SIZE = 150
# Preserve the original masks for the first benchmark. After QC, try None.
NITER = 500
RESAMPLE = True  # Set False only after comparing masks/ROIs on sample images.
NORM3D = False
INVERT = False
NORM_PERCENTILE = [2, 99.8]
SELECTED_CHANNELS: List[int] = []
BATCH_SIZE = 48

# IMPORTANT: turn OFF while debugging segmentation.
# After masks look good, set this to ~48.5 to match your old 23 µm cutoff on the new microscope.
MIN_EQUIV_DIAMETER_UM = 26


# ============================================================
# Helpers
# ============================================================
def select_channels(img: np.ndarray, ch: List[int]) -> np.ndarray:
    if img.ndim == 2 or not ch:
        return img

    if img.ndim == 3 and img.shape[-1] >= (max(ch) + 1):
        return img[..., ch]

    if img.ndim == 3 and img.shape[0] >= (max(ch) + 1):
        x = img[ch, ...]
        return np.moveaxis(x, 0, -1)

    raise ValueError(f"Cannot select channels {ch} from image with shape {img.shape}")


def _to_uint8(x: np.ndarray) -> np.ndarray:
    x = x.astype(np.float32, copy=False)
    lo, hi = np.percentile(x, NORM_PERCENTILE)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.zeros_like(x, dtype=np.uint8)
    x = (x - lo) / (hi - lo)
    x = np.clip(x, 0, 1)
    return (x * 255).astype(np.uint8)


@dataclass
class PreparedImage:
    path: Path
    img_in: np.ndarray
    gray: np.ndarray


def run_cellpose_batch(
    batch: List[PreparedImage],
    model: models.CellposeModel,
) -> List[np.ndarray]:
    """Segment 1 or more same-sized 2D images, keeping every Z-plane separate.

    For multiple images, the 4D N,Y,X,1 array lets Cellpose batch its GPU
    patches across images. A Python list would make Cellpose evaluate one image
    at a time and would not give the same acceleration.
    """
    if not batch:
        return []

    if len(batch) == 1:
        # Identical grayscale API shape to the original script.
        model_input = batch[0].gray
    else:
        sizes = {image.gray.shape for image in batch}
        if len(sizes) != 1:
            raise ValueError("A GPU batch must contain equally sized 2D images")
        # Last dimension is the one grayscale channel, not a Z-axis.
        model_input = np.stack([b.gray[..., np.newaxis] for b in batch])

    options = dict(
        diameter=DIAMETER,
        flow_threshold=FLOW_THRESHOLD,
        cellprob_threshold=CELLPROB_THRESHOLD,
        min_size=MIN_SIZE,
        niter=NITER,
        normalize=True,
        do_3D=False,
        batch_size=BATCH_SIZE,
        resample=RESAMPLE,
    )
    if len(batch) > 1:
        options["channel_axis"] = -1

    masks, _flows, _styles = model.eval(model_input, **options)
    if len(batch) == 1:
        output = [np.asarray(masks)]
    else:
        stacked_masks = np.asarray(masks)
        if stacked_masks.ndim != 3 or stacked_masks.shape[0] != len(batch):
            raise ValueError(
                f"Expected {len(batch)} independent masks, "
                f"received {stacked_masks.shape}"
            )
        output = [stacked_masks[i] for i in range(len(batch))]

    for item, mask in zip(batch, output):
        if mask.shape != item.gray.shape:
            raise ValueError(
                f"Mask for {item.path.name} has shape {mask.shape}, "
                f"expected {item.gray.shape}"
            )
    return [m.astype(np.int32, copy=False) for m in output]


def filter_labels_by_min_equiv_diameter_um(
    labels: np.ndarray,
    min_d_um: float,
) -> np.ndarray:
    if min_d_um <= 0:
        return labels
    if labels is None or labels.size == 0 or labels.max() == 0:
        return labels

    min_d_px = float(min_d_um) / float(PIXEL_SIZE_UM)
    out = labels.copy()

    for p in regionprops(out):
        area = float(p.area)
        eq_d_px = np.sqrt(4.0 * area / np.pi)
        if eq_d_px < min_d_px:
            out[out == p.label] = 0

    out, _, _ = relabel_sequential(out)
    return out


def density_cells_per_cm2(cells: int, hw: Tuple[int, int]) -> float:
    h, w = hw
    area_um2 = (h * w) * (PIXEL_SIZE_UM ** 2)
    area_cm2 = area_um2 / 1e8
    return float(cells / area_cm2) if area_cm2 > 0 else float("nan")

class FileStabilityTracker:
    """Observe all TIFF files concurrently instead of waiting 3s per file.

    A file becomes eligible only when both size and modification timestamp
    remain unchanged for FILE_STABLE_SECONDS. A new change restarts its timer.
    """

    def __init__(self) -> None:
        self.observed: Dict[Path, Tuple[Tuple[int, int], float]] = {}

    def stable_files(
        self,
        candidates: List[Path],
        processed: set,
        retry_after: Dict[str, float],
    ) -> List[Path]:
        now = time.monotonic()
        ready: List[Path] = []

        for path in candidates:
            if path.name in processed:
                self.observed.pop(path, None)
                continue
            try:
                stat = path.stat()
            except (FileNotFoundError, PermissionError):
                self.observed.pop(path, None)
                continue
            if stat.st_size <= 0:
                continue

            signature = (stat.st_size, stat.st_mtime_ns)
            previous = self.observed.get(path)
            if previous is None or previous[0] != signature:
                self.observed[path] = (signature, now)
                continue
            if now - previous[1] < FILE_STABLE_SECONDS:
                continue
            if retry_after.get(path.name, 0.0) > now:
                continue
            ready.append(path)

        return ready

# ============================================================
# ROI creation and overlay polygons
# ============================================================
def labels_to_rois_and_polygons(
    labels: np.ndarray,
    min_points: int = 6,
) -> Tuple[List[ImagejRoi], List[np.ndarray]]:
    rois: List[ImagejRoi] = []
    polys: List[np.ndarray] = []

    ids = np.unique(labels)
    ids = ids[ids > 0]

    for lab_id in ids:
        mask = (labels == lab_id).astype(np.uint8)
        if int(mask.sum()) < 5:
            continue

        contours = find_contours(mask, level=0.5)
        if not contours:
            continue

        c = max(contours, key=lambda a: a.shape[0])
        if c.shape[0] < min_points:
            continue

        ys = c[:, 0]
        xs = c[:, 1]

        xs_i = np.clip(
            np.rint(xs),
            0,
            labels.shape[1] - 1,
        ).astype(np.int16)

        ys_i = np.clip(
            np.rint(ys),
            0,
            labels.shape[0] - 1,
        ).astype(np.int16)

        pts = np.column_stack([xs_i, ys_i])  # x, y
        roi = ImagejRoi.frompoints(pts)
        roi.name = f"cell_{int(lab_id):05d}"
        rois.append(roi)

        polys.append(pts.astype(np.int32))

    return rois, polys


def save_rois_zip(zip_path: Path, rois: List[ImagejRoi]) -> int:
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as zf:
        for roi in rois:
            zf.writestr(f"{roi.name}.roi", roi.tobytes())

    return len(rois)


def save_label_mask_tiff(tiff_path: Path, labels: np.ndarray) -> None:
    """Save the *filtered*, per-image labeled mask used for counts and ROIs.

    Zero is background and each positive integer is a different object.
    This is a label image, not an 8-bit preview: labels must not be scaled.
    """
    if labels.ndim != 2 or np.any(labels < 0):
        raise ValueError("Expected a nonnegative 2D Cellpose label mask")

    max_label = int(labels.max()) if labels.size else 0
    if max_label > np.iinfo(np.uint32).max:
        raise ValueError(f"Mask label {max_label} does not fit in uint32")
    dtype = np.uint16 if max_label <= np.iinfo(np.uint16).max else np.uint32

    tiff_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = tiff_path.with_name(f".{tiff_path.stem}.partial.tiff")
    try:
        write_tiff(
            temporary_path,
            labels.astype(dtype, copy=False),
            photometric="minisblack",
        )
        os.replace(temporary_path, tiff_path)
    finally:
        temporary_path.unlink(missing_ok=True)


# ============================================================
# Plate mapping
# ============================================================
def numbered_image_to_well(image_path: Path) -> str:
    """
    Convert Tile-numbered image filenames into standard
    96-well plate positions.

    Examples:
        Tile000001.tif -> A1
        Tile000012.tif -> A12
        Tile000013.tif -> B1
        Tile000024.tif -> B12
        Tile000096.tif -> H12
    """
    stem = image_path.stem

    if not stem.startswith("Tile"):
        raise ValueError(
            f"Expected a filename such as 'Tile000001.tif', "
            f"but received '{image_path.name}'"
        )

    number_text = stem[len("Tile"):]

    if not number_text.isdigit():
        raise ValueError(
            f"Expected numeric characters after 'Tile' in "
            f"'{image_path.name}'"
        )

    image_number = int(number_text)

    if not 1 <= image_number <= 96:
        raise ValueError(
            f"Image number must be between 1 and 96, "
            f"but received {image_number} from '{image_path.name}'"
        )

    zero_based_index = image_number - 1

    row_index = zero_based_index // 12
    column_number = (zero_based_index % 12) + 1

    row_letter = PLATE_ROWS[row_index]

    return f"{row_letter}{column_number}"


# ============================================================
# Analysis per image, after GPU inference
# Counts come from labeled masks, not from successful ROI export.
# ============================================================
def analyze_segmented_image(
    img_in: np.ndarray,
    labels: np.ndarray,
) -> Dict[str, Any]:
    labels = filter_labels_by_min_equiv_diameter_um(
        labels,
        MIN_EQUIV_DIAMETER_UM,
    )

    cells = int(labels.max())

    if SAVE_ROIS or SAVE_DEBUG_PLOTS:
        rois, polys = labels_to_rois_and_polygons(labels)
    else:
        rois, polys = [], []

    h, w = labels.shape[:2]
    dens = density_cells_per_cm2(cells, (h, w))
    area_um2 = float(h * w) * (PIXEL_SIZE_UM ** 2)

    return {
        "img_in": img_in,
        "labels": labels,
        "rois": rois,
        "polys": polys,
        "cells": cells,
        "density": float(dens),
        "area_um2": float(area_um2),
        "h": int(h),
        "w": int(w),
    }


# ============================================================
# Fast outline preview, exact pixel dimensions, ROI polygons overlaid
# ============================================================
def save_debug_plot_with_rois(
    well: str,
    img_in: np.ndarray,
    polys: List[np.ndarray],
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    if img_in.ndim == 2:
        disp = img_in
    else:
        disp = np.max(img_in, axis=-1)

    disp_u8 = _to_uint8(disp)

    overlay = Image.fromarray(disp_u8).convert("RGB")
    draw = ImageDraw.Draw(overlay)

    # Golden-angle hue spacing gives visually distinct neighboring cell colors.
    # Colors are for preview only: mask TIFFs retain integer label IDs.
    for cell_index, pts in enumerate(polys):
        if pts.shape[0] < 2:
            continue
        hue = (0.03 + cell_index * 0.618033988749895) % 1.0
        rgb = colorsys.hsv_to_rgb(hue, 0.93, 1.0)
        outline_color = tuple(round(value * 255) for value in rgb)
        coords = [(int(x), int(y)) for x, y in pts]
        draw.line(coords + [coords[0]], fill=outline_color, width=2)

    overlay.save(out_dir / f"{well}.png", format="PNG", compress_level=1)


# ============================================================
# Concurrent file readiness, loading and monitoring
# ============================================================
def list_tiffs() -> List[Path]:
    """Include all channels, all Z planes and both TIFF extensions."""
    return natsorted([
        p for p in INPUT_FOLDER.iterdir()
        if p.is_file() and p.suffix.lower() in {".tif", ".tiff"}
    ])


def prepare_batch(
    ready_paths: List[Path],
    tracker: FileStabilityTracker,
    retry_after: Dict[str, float],
    image_limit: int,
) -> List[PreparedImage]:
    """Read at most image_limit complete, equal-sized TIFF images."""
    prepared: List[PreparedImage] = []
    selected_shape: Optional[Tuple[int, int]] = None

    for path in ready_paths:
        if len(prepared) >= image_limit:
            break

        try:
            # Guard against a file being rewritten between the stability scan
            # and decoding. Such images are retried, never marked processed.
            expected = tracker.observed[path][0]
            before = path.stat()
            signature_before = (before.st_size, before.st_mtime_ns)
            if signature_before != expected:
                continue

            raw = cpio.imread(str(path))
            after = path.stat()
            signature_after = (after.st_size, after.st_mtime_ns)
            if signature_after != signature_before:
                raise IOError("TIFF changed while being read; retrying")

            img_in = select_channels(raw, SELECTED_CHANNELS)
            if img_in.ndim == 2:
                gray = img_in
            elif img_in.ndim == 3:
                # Preserve the original script's per-TIFF channel projection.
                gray = np.max(img_in, axis=-1)
            else:
                raise ValueError(f"Unexpected TIFF shape: {img_in.shape}")

            if gray.ndim != 2:
                raise ValueError(f"Expected a 2D image, received {gray.shape}")
            if selected_shape is not None and gray.shape != selected_shape:
                # Leave this TIFF pending. It will be processed in a compatible
                # batch, potentially as a single image on the next pass.
                continue

            selected_shape = gray.shape
            prepared.append(PreparedImage(path=path, img_in=img_in, gray=gray))

        except (OSError, ValueError, KeyError, IndexError) as exc:
            retry_after[path.name] = time.monotonic() + RETRY_DELAY_SECONDS
            print(f"[RETRY] {path.name}: {exc}", flush=True)

    return prepared


def main():
    if not INPUT_FOLDER.is_dir():
        raise FileNotFoundError(f"Etaluma capture folder not found: {INPUT_FOLDER}")

    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_FOLDER / "density_per_well.csv"
    timing_path = OUTPUT_FOLDER / "batch_timings.csv"
    debug_dir = OUTPUT_FOLDER / "debug_plots"
    roi_dir = OUTPUT_FOLDER / "rois"
    mask_dir = OUTPUT_FOLDER / "masks"

    os.environ["CUDA_VISIBLE_DEVICES"] = GPU
    use_gpu = core.use_gpu()
    print(f"GPU activated: {use_gpu}", flush=True)
    if not use_gpu:
        print("WARNING: GPU unavailable; segmentation will likely be much slower.")

    model = models.CellposeModel(
        gpu=use_gpu,
        pretrained_model="cpdino",
    )

    fieldnames = [
        "well",
        "image_name",
        "cells",
        "density_cells_per_cm2",
        "area_um2",
    ]

    processed = set()
    if csv_path.exists() and csv_path.stat().st_size > 0:
        with open(csv_path, "r", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("image_name"):
                    processed.add(row["image_name"])

    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    if write_header:
        with open(csv_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()

    timing_fields = [
        "timestamp_utc", "batch_images", "gpu_inference_seconds",
        "total_batch_seconds", "images_per_second", "image_names",
    ]
    write_timing_header = (
        not timing_path.exists() or timing_path.stat().st_size == 0
    )
    if write_timing_header:
        with open(timing_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=timing_fields).writeheader()

    tracker = FileStabilityTracker()
    retry_after: Dict[str, float] = {}
    current_batch_limit = IMAGES_PER_GPU_BATCH

    print(f"Watching for TIFF files in: {INPUT_FOLDER}")
    print(f"Saving results in: {OUTPUT_FOLDER}")
    print(f"Resuming: {len(processed)} images recorded in existing CSV")
    print(f"GPU image batch size: {current_batch_limit}; all channels and Z-planes")
    print("Press Ctrl+C to stop safely.", flush=True)

    try:
        while True:
            paths = list_tiffs()
            ready_paths = tracker.stable_files(paths, processed, retry_after)
            prepared = prepare_batch(
                ready_paths, tracker, retry_after, current_batch_limit
            )
            if not prepared:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            started = time.perf_counter()
            try:
                print(
                    f"[GPU] Segmenting {len(prepared)} images: "
                    + ", ".join(p.path.name for p in prepared),
                    flush=True,
                )
                inference_started = time.perf_counter()
                masks = run_cellpose_batch(prepared, model)
                inference_seconds = time.perf_counter() - inference_started
            except Exception as exc:
                print(f"[GPU ERROR] {exc}", flush=True)
                if len(prepared) > 1:
                    # Safely fall back if an older Cellpose version rejects
                    # batched arrays, or the available GPU memory is too small.
                    print("[GPU] Falling back to one image per call.", flush=True)
                    current_batch_limit = 1
                    del prepared
                    gc.collect()
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass
                else:
                    retry_after[prepared[0].path.name] = (
                        time.monotonic() + RETRY_DELAY_SECONDS
                    )
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            successful_names = []
            for image, labels in zip(prepared, masks):
                image_path = image.path
                try:
                    item = analyze_segmented_image(image.img_in, labels)

                    if SAVE_DEBUG_PLOTS:
                        save_debug_plot_with_rois(
                            well=image_path.stem,
                            img_in=item["img_in"],
                            polys=item["polys"],
                            out_dir=debug_dir,
                        )
                    if SAVE_ROIS:
                        save_rois_zip(
                            roi_dir / f"{image_path.stem}.zip",
                            item["rois"],
                        )
                    if SAVE_MASK_TIFFS:
                        save_label_mask_tiff(
                            mask_dir / f"{image_path.stem}_masks.tiff",
                            item["labels"],
                        )

                    result = {
                        "well": image_path.stem,
                        "image_name": image_path.name,
                        "cells": item["cells"],
                        "density_cells_per_cm2": item["density"],
                        "area_um2": item["area_um2"],
                    }
                    with open(csv_path, "a", newline="") as f:
                        csv.DictWriter(f, fieldnames=fieldnames).writerow(result)

                    # Mark processed only after all requested files and the
                    # CSV row are successfully written.
                    processed.add(image_path.name)
                    tracker.observed.pop(image_path, None)
                    retry_after.pop(image_path.name, None)
                    successful_names.append(image_path.name)
                    print(
                        f"Finished {image_path.name}: {item['cells']} objects",
                        flush=True,
                    )
                except Exception as exc:
                    retry_after[image_path.name] = (
                        time.monotonic() + RETRY_DELAY_SECONDS
                    )
                    print(f"[RETRY] Export failed for {image_path.name}: {exc}")

            total_seconds = time.perf_counter() - started
            with open(timing_path, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=timing_fields).writerow({
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "batch_images": len(prepared),
                    "gpu_inference_seconds": round(inference_seconds, 3),
                    "total_batch_seconds": round(total_seconds, 3),
                    "images_per_second": round(
                        len(successful_names) / total_seconds, 4
                    ) if total_seconds > 0 else 0,
                    "image_names": "|".join(successful_names),
                })
            print(
                f"[TIMING] GPU={inference_seconds:.2f}s, "
                f"batch_total={total_seconds:.2f}s, "
                f"saved={len(successful_names)}, "
                f"completed_total={len(processed)}",
                flush=True,
            )

    except KeyboardInterrupt:
        print("\nMonitoring stopped.")
        print(f"Results saved to: {csv_path}")


if __name__ == "__main__":
    main()
