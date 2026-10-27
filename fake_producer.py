#!/usr/bin/env python3
"""Synthetic LumaViewPro stand-in.

Writes 2048x2048 uint16 TIFFs into a directory tree at a configurable rate.
Every Nth image is written in slow chunks with sleeps in between, so the
backend's "size unchanged across two polls" guard is actually exercised: the
dashboard must never count a half-written file.

    python fake_producer.py --out ./data/input --rate 2 --count 400
"""
import argparse
import io
import random
import time
from pathlib import Path

import numpy as np
import tifffile

WELLS = [f"{r}{c:02d}" for r in "ABCDEFGH" for c in range(1, 13)]


def synth(h: int, w: int, rng: random.Random) -> np.ndarray:
    im = np.random.default_rng(rng.randrange(2**32)).integers(
        200, 900, size=(h, w), dtype=np.uint16
    )
    yy, xx = np.mgrid[0:h, 0:w]
    for _ in range(rng.randint(30, 90)):
        cy, cx = rng.randrange(h), rng.randrange(w)
        r = rng.randint(12, 34)
        blob = (yy - cy) ** 2 + (xx - cx) ** 2 < r * r
        im[blob] = np.clip(im[blob].astype(np.int32) + rng.randint(2000, 9000), 0, 65535)
    return im


def encode(im: np.ndarray) -> bytes:
    buf = io.BytesIO()
    tifffile.imwrite(buf, im)
    return buf.getvalue()


def write_fast(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_slow(path: Path, payload: bytes, chunks: int, pause: float) -> None:
    """Write incrementally, like an acquisition still flushing to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    step = max(1, len(payload) // chunks)
    with open(path, "wb") as f:
        for i in range(0, len(payload), step):
            f.write(payload[i:i + step])
            f.flush()
            time.sleep(pause)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="./data/input")
    p.add_argument("--rate", type=float, default=1.0, help="images per second")
    p.add_argument("--count", type=int, default=200, help="0 = run forever")
    p.add_argument("--width", type=int, default=2048)
    p.add_argument("--height", type=int, default=2048)
    p.add_argument("--z", type=int, default=20)
    p.add_argument("--channels", type=int, default=2)
    p.add_argument("--tiles", type=int, default=4, help="2x2 tiling = 4")
    p.add_argument("--slow-every", type=int, default=7,
                   help="write every Nth image in slow chunks (0 = never)")
    p.add_argument("--slow-chunks", type=int, default=8)
    p.add_argument("--slow-pause", type=float, default=1.5)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()

    out = Path(a.out).expanduser().resolve()
    rng = random.Random(a.seed)
    delay = 1.0 / a.rate if a.rate > 0 else 0.0
    n = 0
    t_start = time.time()
    print(f"writing {a.width}x{a.height} uint16 TIFFs to {out} at {a.rate}/s", flush=True)
    while a.count == 0 or n < a.count:
        i = n
        well = WELLS[(i // (a.z * a.channels * a.tiles)) % len(WELLS)]
        tile = (i // (a.z * a.channels)) % a.tiles
        ch = (i // a.z) % a.channels
        z = i % a.z
        name = f"{well}_T{tile}_C{ch}_Z{z:03d}_{i:06d}.tiff"
        path = out / well / f"tile{tile}" / name
        payload = encode(synth(a.height, a.width, rng))
        t0 = time.time()
        if a.slow_every and n % a.slow_every == a.slow_every - 1:
            print(f"  [slow] {path.name} in {a.slow_chunks} chunks", flush=True)
            write_slow(path, payload, a.slow_chunks, a.slow_pause)
        else:
            write_fast(path, payload)
        n += 1
        if n % 25 == 0:
            el = time.time() - t_start
            print(f"  {n} images, {n / el:.2f}/s average", flush=True)
        time.sleep(max(0.0, delay - (time.time() - t0)))
    print(f"done: {n} images in {time.time() - t_start:.0f}s", flush=True)


if __name__ == "__main__":
    main()
