# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two FastAPI + SQLite + React apps that watch the folder Etaluma LumaViewPro writes TIFFs
into, segment them with Cellpose DINO (`pretrained_model='cpdino'`), and show it live.
LumaViewPro itself is never modified — these apps only read its output.

- `backend/` + `frontend/` — **the dashboard** (port 8000, `MM_*` env). File-centric; the
  question it answers is "does the segmentation backlog grow?"
- `station/` + `station/frontend/` — **the station** (port 8100, `MM_ST_*` env). Run-centric:
  a run is a plate, every image and mask belongs to one. Adds a 96-well plate map, run
  history with chart replay, and webhook alerts. Separate DB, separate frontend.
- `preflight.py` — fresh-workstation checks (GPU kernels, dinov3, dirs, port, throughput).
- `fake_producer.py` — synthetic acquisition for local testing.

The two apps must **never share an `--input-dir`**: each runs its own Cellpose worker and
they would both segment the same TIFFs.

## Commands

```bash
# dashboard
python -m backend.main --input-dir ./data/input --output-dir ./data/masks \
  --db-path ./data/monitor.sqlite --port 8000 [--dry-run]
cd frontend && npm install && npm run build   # backend then serves frontend/dist
cd frontend && npm run dev                    # :5173, proxies /api + /ws to :8000

# station
python -m station.main --input-dir ./data/station/input --db-path ./data/station.sqlite
cd station/frontend && npm run dev            # :5174, proxies to :8100

# tests — there is no pytest; the only test suite is:
python -m station.selftest                    # alerts, acquisition lock, ETAs
# single test: edit main() in station/selftest.py, or call e.g.
python -c "import tempfile,pathlib,station.selftest as s; s.test_eta(pathlib.Path(tempfile.mkdtemp()))"

# before a real run
python preflight.py --input-dir /data/lumaviewpro/output --output-dir /data/masks
python preflight.py --input-dir ... --throughput   # ~1.1 GB model download on first use

# synthetic load (every 7th file written in slow chunks to exercise the partial-file guard)
python fake_producer.py --out ./data/input --rate 2 --count 400 --slow-every 7
```

`--dry-run` writes a trivial threshold mask instead of calling Cellpose and labels itself
`dry-run` in the UI — use it whenever Cellpose isn't installed or isn't the point.

## Architecture

Each app is a **single process with two background threads**: the segmentation worker and
the telemetry sampler, both started from the FastAPI `lifespan` in `app.py`. The DB is one
SQLite connection serialized behind a lock (`db.py`), so all state — discovered files, job
state, telemetry, the global pause flag — survives a restart and the worker resumes.

`create_app(cfg)` wires everything: `Database` → `Worker` → `acquisition.probe()` →
`JobManager`/`RunManager` → `Sampler`. The WebSocket `/ws` pushes the sampler's snapshot
every 2 s; `GET /api/status` returns the same object.

**`StationWorker` subclasses `backend.worker.Worker`.** `scan()` is inherited verbatim —
the partial-file guard and backlog accounting live there and must not be copied into the
station. Only run-aware hooks (`_job_for_new_files`, `segment_one_batch`, `_globally_paused`,
`snapshot`) are overridden. Fix that logic once and both apps get it.

### Invariants that look like bugs if you don't know them

- **Partial-file guard**: a TIFF is queued only when its size is identical on two consecutive
  polls *and* has held that size for `--stable-seconds` (default 2× poll interval), with a
  truncated-read retry (5 attempts) as a last net. Both conditions are needed — a writer that
  stalls for longer than one poll interval defeats the two-poll rule alone.
- **Cancelled work is `set_aside`, not `backlog`** and stays `pending`, never `failed`.
  Counting it would hold the falling-behind banner red forever.
- **ETAs are ranges or nothing.** Under three completed batches the API reports
  `not enough data` and the UI shows no number. A fabricated ETA is worse than none here.
  Queued items show no start estimate if anything ahead of them has none.
- **Acquisition control is stubbed.** Every hardware verb in `backend/acquisition.py` and
  `station/acquisition.py` is a `TODO(headless)`; `probe()` falls back to `ObservedAcquisition`
  on any doubt, and in Observed mode the UI omits the controls (not disables them) and the
  backend answers `409`. Don't implement these without hardware to verify against.
- **The acquisition lock is one mutex in the backend** (`JobManager.create_acquisition`,
  `RunManager.create`) — two simultaneous browsers get one job and one `409`. Never move it
  to the UI.
- **No filename pattern or image dimension is hardcoded.** The scanner globs `**/*.tif[f]`
  case-insensitively; well labels are inferred from path components (`station/wells.py`) and
  images that say nothing are `unplaced`, never guessed into A01.

### Auth

Every `GET` is open. Every state-changing `POST` needs `X-Auth-Token` matching
`MM_AUTH_TOKEN` / `MM_ST_AUTH_TOKEN`, compared with `secrets.compare_digest`. There is no
user system and should not be one for a lab instrument. Unset token = open POSTs, warned at
startup and in the UI.

### Frontend

React 18 + Vite + recharts on **Gentelella v4** — a self-contained design system, no
Bootstrap, no jQuery. Dark by default with a light mode applied before first paint (no flash),
remembered per browser. Gentelella's Sass partials `@use` each other by bare name, so
`node_modules` must stay on the Sass `loadPaths` in `vite.config.js`. `useLive.js` holds the
WebSocket reconnect (exponential backoff 1 s → 30 s) and the 15 s stale-data banner.

## Environment gotchas

- Python 3.11 or 3.12 (torch/cellpose wheels).
- `dinov3` is required by `cpdino` but is **not a declared cellpose dependency and not on
  PyPI**. Cellpose imports it in a bare `try/except`, so a missing install looks fine until
  model construction fails with `NameError: name 'dinov3_vitl16' is not defined`.
  `pip install git+https://github.com/facebookresearch/dinov3`.
- RTX 5090 is Blackwell (`sm_120`) and needs a CUDA 12.8+ torch build; the default PyPI wheel
  imports fine and dies at the first kernel launch. `preflight.py` checks
  `torch.cuda.get_arch_list()` for exactly this.
- Device is `cuda` → `mps` → `cpu`, shown in the UI header. The M4 Mac mini measures
  ~34.7 s/image at 2048×2048 on MPS — far too slow for a real plate; it is a UI dev box.

Full config tables, API tables, systemd deployment and the Tailscale guidance are in
`README.md` and `station/README.md`; keep them in sync when changing flags or endpoints.
