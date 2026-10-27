# Microscope Monitor

Watches the folder Etaluma LumaViewPro writes TIFFs into, segments them with
Cellpose DINO (`pretrained_model='cpdino'`), and shows the whole thing live in a
browser. LumaViewPro is not modified or touched — this app only reads its output.

The question the dashboard exists to answer: **does the backlog grow?** If one
machine can acquire and segment at the same time, backlog stays flat. If it
can't, the banner at the top turns red and says `SEGMENTATION FALLING BEHIND`.

- Backend: FastAPI + SQLite, single process, two background threads
  (segmenter, telemetry sampler).
- Frontend: React + Vite + recharts, one dark dashboard page.
- Device: `cuda` if available, else `mps`, else `cpu` — shown in the UI header.

## How files are picked up

LumaViewPro writes TIFFs incrementally, so a file is only queued once its size
is **identical on two consecutive polls** (default 5 s apart) **and has held
that size for `--stable-seconds`** (default 2x the poll interval). The second
condition matters: a writer that stalls mid-file for longer than one poll
interval looks exactly like a finished file to the two-poll rule alone — that
is reproducible with `fake_producer.py --slow-pause`. As a last net, if a file
that passed both checks still reads as truncated, it is retried on later polls
and only reported as failed after 5 attempts. A half-written TIFF is never
segmented and never counted as found. Every discovered file gets a row in SQLite
(`pending` / `done` / `failed`), so restarting the service resumes where it left
off and never re-segments anything.

Batches hold up to 16 files, grouped so every image in a batch has identical
dimensions. The model is constructed once at startup and reused. On CUDA OOM the
batch size is halved and retried (logged, and surfaced as `OOM retries` in the
header); the process never dies. Any other per-batch error marks those files
`failed` with the error text and the worker continues.

Masks are written as `.npy`, mirroring the input tree:
`$MM_INPUT_DIR/A01/tile0/x.tiff` → `$MM_OUTPUT_DIR/A01/tile0/x.npy`.

No filename pattern or image dimension is hardcoded anywhere: the scanner globs
`**/*.tif` and `**/*.tiff` case-insensitively and reads dimensions per file.

## Install

### macOS dev box (M4 Mac mini)

Cellpose and torch want a Python that has wheels; use 3.11 or 3.12.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # torch here is the MPS build
cd frontend && npm install && npm run build && cd ..
```

If you just want to see the dashboard without a working cellpose install, run
the backend with `--dry-run` (see below) — it writes a trivial threshold mask
instead of calling Cellpose and labels itself `dry-run` in the UI.

### Ubuntu + RTX 5090

The 5090 is Blackwell (`sm_120`) and needs a CUDA 12.8+ torch build — the
default PyPI wheel will load and then fail at the first kernel launch.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

## Local test (Mac mini)

Three terminals.

1. Backend:

```bash
source .venv/bin/activate && python -m backend.main --input-dir ./data/input --output-dir ./data/masks --db-path ./data/monitor.sqlite --port 8000
```

   Add `--dry-run` to skip Cellpose entirely.

2. Synthetic acquisition — 2048×2048 uint16 TIFFs, every 7th one written in
   slow chunks so the partial-file guard is actually exercised:

```bash
source .venv/bin/activate && python fake_producer.py --out ./data/input --rate 2 --count 400 --slow-every 7
```

3. Dashboard (Vite dev server, proxies `/api` and `/ws` to :8000):

```bash
cd frontend && npm run dev
```

   Open http://localhost:5173. Or skip this and open http://localhost:8000
   if you ran `npm run build` — the backend serves `frontend/dist`.

To watch the banner go red, outrun the GPU:
`python fake_producer.py --out ./data/input --rate 20 --count 0`

Start over: `rm -rf ./data && rm -f specs.txt`

## Real run (Ubuntu workstation)

Start LumaViewPro and let it begin its 96-well run, then:

```bash
source .venv/bin/activate
python -m backend.main \
  --input-dir /data/lumaviewpro/output \
  --output-dir /data/masks \
  --db-path /var/lib/microscope-monitor/monitor.sqlite \
  --specs-path /var/lib/microscope-monitor/specs.txt \
  --port 8000
```

Open `http://<workstation-ip>:8000` from any machine on the lab network.

As a service:

```bash
sudo mkdir -p /var/lib/microscope-monitor && sudo chown lab:lab /var/lib/microscope-monitor
sudo cp deploy/microscope-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now microscope-monitor
journalctl -u microscope-monitor -f
```

## Config

CLI flag / env var / default:

| flag | env | default |
|---|---|---|
| `--input-dir` | `MM_INPUT_DIR` | `./data/input` |
| `--output-dir` | `MM_OUTPUT_DIR` | `./data/masks` |
| `--db-path` | `MM_DB_PATH` | `./data/monitor.sqlite` |
| `--specs-path` | `MM_SPECS_PATH` | `./specs.txt` |
| `--host` | `MM_HOST` | `0.0.0.0` |
| `--port` | `MM_PORT` | `8000` |
| `--batch-size` | `MM_BATCH_SIZE` | `16` |
| `--poll-interval` | `MM_POLL_INTERVAL` | `5` (seconds) |
| `--stable-seconds` | `MM_STABLE_SECONDS` | `0` = 2x poll interval |
| `--telemetry-interval` | `MM_TELEMETRY_INTERVAL` | `2` (seconds) |
| `--model` | `MM_MODEL` | `cpdino` |
| `--dry-run` | `MM_DRY_RUN` | off |

## API

| endpoint | returns |
|---|---|
| `GET /api/status` | current snapshot (counts, rates, ETA, GPU/CPU/RAM/disk, worker state) |
| `GET /api/history?minutes=30` | time series of those snapshots, for the charts |
| `GET /api/specs` | machine spec dump as JSON + rendered text |
| `GET /api/specs.txt` | the same dump as plain text |
| `GET /api/failures` | failed files with error text |
| `GET /api/config` | resolved config, device, model |
| `WS /ws` | a snapshot pushed every 2 s |

## Specs

On startup the app captures `uname -a`, `lscpu`, `free -h`, `lsblk`, `df -h`,
the full `nvidia-smi` plus the driver version, torch version / CUDA build /
device name / compute capability, the cellpose version, and which block device
holds the input directory (`findmnt -T`, plus `lsblk` for that device). It is
stored in SQLite, served at `/api/specs`, written to `specs.txt`, and the
dashboard shows it in a panel with a **Copy** button.

On macOS the Linux-only commands report themselves unavailable and
`sysctl machdep.cpu.brand_string` / `system_profiler SPHardwareDataType` are
captured instead, so a dev-box dump is still useful.
