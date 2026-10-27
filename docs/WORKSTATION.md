# Lab workstation

The machine the Etaluma microscope and these apps run on, as found on
2026-10-27. Update this when hardware, drivers or environments change.

## Hardware

| | |
|---|---|
| Host | `neil-linlab-GamingPC` |
| CPU | Intel Core Ultra 9 285K, 24 cores / 24 threads |
| RAM | 62 GiB |
| GPU | NVIDIA GeForce RTX 5090, 32 GB VRAM, compute capability 12.0 (Blackwell, `sm_120`) |
| NVIDIA driver | 580.178.04 (CUDA 13.0) |
| Disk | Samsung 990 EVO Plus 2 TB NVMe, one volume for OS, LVP captures and outputs (1.3 TB free) |
| OS | Ubuntu 24.04.5 LTS, kernel 7.0.0-38-generic |

## Software

| | |
|---|---|
| LumaViewPro | 4.0.0-beta at `~/Projects/Etaluma_API/LumaViewPro-4.0.0-beta`, its own `venv/`, started with `./venv/bin/python lumaviewpro.py` |
| LVP capture root | `…/LumaViewPro-4.0.0-beta/capture` (`live_folder` in `data/current.json`); protocol runs go to `capture/ProtocolData/YYYYMMDD_HHMMSS/` |
| LVP REST API | present in settings, **disabled**, default port **8000**. That is the dashboard's default port too; if the REST API is ever enabled, move one of them. |
| Python env for these apps | `~/cellpose-cpdino-env` (Python 3.12.3): cellpose 4.2.1.1, torch 2.14.1+cu130 (has `sm_120`), dinov3, tifffile 2026.10.15, roifile, scikit-image 0.26, fastapi, uvicorn |
| Other env | `~/venvs/cellpose`: cellpose 4.2.1.1, torch 2.11.0+cu128. Works for segmentation; has no web packages. |
| Node.js | 22 LTS unpacked in `~/.local/node/` (not on `PATH` by default): `export PATH=~/.local/node/node-v22.23.4-linux-x64/bin:$PATH` before `npm run build` |
| Missing | `gh`, `curl`. Python's `urllib` works for HTTP checks. |

## What LumaViewPro writes here

Per protocol run, flat in one folder:

- `A1_PC_TA1_Z0_0000.tiff`: well, color (`PC` or `Green`), tile (`TA1 TA2 TB1 TB2`, a
  2×2 tiling), Z-slice (0–19), scan number
- 1900×1900 `uint8`, LZW. `Green` is a palette TIFF (index = intensity); `PC` is
  greyscale. Written in place with `TiffWriter`, not via a temp file, so the
  partial-file guard is needed.
- `unsaved_protocol.tsv` (the plan; 15,360 image steps for a full plate =
  96 wells × 2 colors × 4 tiles × 20 Z) and `protocol_record.tsv` (one row per
  image as it lands, with a timestamp).
- Objective 4x Olympus; scale 3.38 mm = 1800 px (1.878 µm/px), as set in
  `backend/analysis.py`.

## Measured rates (2026-10-27, run `20261027_155550`)

- **Acquisition:** about 1.65 images/s (PC frames every ~0.47 s, Green every
  ~0.70 s), so a full plate takes about 2.6 h.
- **Standalone script** (`Etaluma_Live_Segmentation_Optimized (2).py`,
  2 images per GPU call, alone on the GPU):
  - GPU inference: median 0.68 s/image.
  - End to end: 0.86 s/image, about 0.78 images/s, so it falls about 2× behind
    acquisition.
  - 44% of its time went to whole-frame ROI tracing. That costs up to ~12 s on
    a frame with 1,600 cells; `backend/analysis.py` does the same work in
    ~0.2 s with identical output.
- **Segmentation results:** with the validated settings, PC frames return a
  median of 0 cells (max 21). The settings are tuned for Green, yet PC frames
  take about half the GPU time.
- **Station vs script:** the station's masks were byte-identical to the
  script's for every image both processed.

## What normal looks like

Also shown live on the station Dashboard ("What normal looks like" card, which
marks each row **normal** or **check**). Measured on this machine during a
full-plate run (96 wells, PC + Green, 20 Z) with the standalone script also
running on the GPU. A different protocol, objective, or another program on the
GPU changes these numbers.

| What | Normal | Look into it when |
|---|---|---|
| GPU load | 80–100% while images are waiting; ~0% when the backlog is empty. It swings 0↔100% inside every batch, so judge the average. | ~0% while images are waiting and nothing is paused: the worker is stuck |
| GPU temperature | 55–75 °C under load (peak seen 73 °C) | above 83 °C for minutes: check fans and room |
| GPU power | 400–600 W segmenting (touching the 600 W limit is normal); 80–120 W idle | idle power while images are waiting |
| GPU memory | ≈5.3 GB per Cellpose process: 5.3 GB with the station alone, ≈10.7 GB with the standalone script too | above 28 of 32 GB, or "CUDA OOM" |
| CPU | 10–25%; brief ~90% at startup while existing images are read | above 80% for minutes |
| RAM | ≈20% (12 of 62 GB) | above 80% |
| Disk writes | 5–50 MB/s during acquisition; reads ≈0 (served from memory) | free space under 50 GB (alert fires) |
| Time between images | ≈0.6 s mean (PC ~0.47 s, Green ~0.70 s); longer at well moves | mean above 1 s for minutes during a z-stack: acquisition has slowed |
| Segmentation speed | ≈0.9 img/s sharing the GPU with the script (≈1.1 s per image). The microscope makes ≈1.65 img/s, so "Falling behind" during a plate is expected with these settings, and the backlog drains after acquisition ends. | 0 img/s while images are waiting and nothing is paused |
