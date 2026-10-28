# Microscope Monitor

Watches the folder Etaluma LumaViewPro writes TIFFs into, segments them with
Cellpose DINO (`pretrained_model='cpdino'`), and shows the whole thing live in a
browser. LumaViewPro is not modified or touched — this app only reads its output.

The question the dashboard exists to answer: **does the backlog grow?** If one
machine can acquire and segment at the same time, backlog stays flat. If it
can't, the banner at the top turns red and says `SEGMENTATION FALLING BEHIND`.

It also tracks the two queues the test actually has — the microscope's
acquisition run and this app's segmentation work — with pause, resume, cancel
and ETAs, and is reachable from off site.

- Backend: FastAPI + SQLite, single process, two background threads
  (segmenter, telemetry sampler).
- Frontend: React + Vite + recharts on Gentelella v4, which is a self-contained
  design system — its own tokens, grid and components, no Bootstrap and no
  jQuery. Dark by default with a light mode; the choice is remembered per
  browser and applied before first paint, so a reload never flashes.
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

Cellpose and torch want a Python that has wheels; use 3.11 or 3.12.

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
pip install git+https://github.com/facebookresearch/dinov3   # required by cpdino
cd frontend && npm install && npm run build && cd ..
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### The cpdino / DINOv3 trap

`cellpose` imports its DINOv3 backbone inside a bare `try/except` that only
logs a warning (`cellpose/vit.py`), so a missing `dinov3` package looks fine at
import time and then fails at model construction with:

```
NameError: name 'dinov3_vitl16' is not defined
```

`dinov3` is not a declared cellpose dependency and is not on PyPI. Install it
from git, as above, and verify before a real run:

```bash
python -c "from dinov3.hub.backbones import dinov3_vitl16; print('dinov3 ok')"
```

The cpdino weights themselves are a ~1.1 GB download on first use, cached in
`~/.cellpose`. Pull them once before the acquisition run, not during it.

## Station — the run-centric app

`station/` is a **separate app** for monitoring and controlling the microscope
and the segmentation from anywhere, including off campus, for unattended
overnight runs. Where this dashboard is organised around files, Station is
organised around **runs**: a run is a plate, and every image and mask belongs
to one. It adds a 96-well plate map, run history with chart replay, and
webhook alerts.

Different port (8100), different database, different frontend. This dashboard
is untouched. See `station/README.md`.

```bash
python -m station.main --input-dir /data/lumaviewpro/output --db-path ./data/station.sqlite
```

As a service: `deploy/microscope-station.service`, the same way as this one.

> Do not point both apps at the same `--input-dir` at once: each runs its own
> segmenter and they would both segment the same TIFFs.

Station's worker subclasses this one, so the partial-file guard and backlog
accounting are inherited rather than copied and cannot drift.

The capacity-study instrumentation is driven from Station, but the parts that
belong to the worker live here and this dashboard gets them too: the
**inter-arrival metric** in `Worker.scan()`, the **phase label** and the
**batch-size/phase recording** in `Worker._record_batch()`, and the **disk
read/write throughput** in `backend/telemetry.py`. The phase store, the
per-phase report and its markdown are in `backend/experiment.py`. The
dashboard's samples carry `phase`, `inter_arrival_mean_s`,
`inter_arrival_p95_s`, `disk_read_mbs` and `disk_write_mbs`; the endpoints
that set a phase, change the batch size and render the report are Station's
only — see `station/README.md`, "Capacity and tuning study".

## The two queues

Two kinds of work, tracked separately, both in the `jobs` table.

**Acquisition** moves physical hardware, so at most one acquisition job may be
active at a time. That lock is enforced in the backend, inside one mutex, not
in the UI: if two browsers press the button at the same moment, one gets the
job and the other gets `409`.

**Segmentation** runs concurrently with acquisition and is created
automatically — files discovered by the scanner join the open segmentation
job, and a new one opens once the previous has drained. Nobody queues these
by hand.

### Acquisition control is conditional

Whether LumaViewPro can be driven headless is **not confirmed**. The app
probes at startup and picks one of two adapters (`backend/acquisition.py`):

| adapter | when | what it does |
|---|---|---|
| `ObservedAcquisition` | the default | No control at all. Progress is inferred by counting TIFFs against an expected total you type in. |
| `ControlledAcquisition` | `--enable-acquisition-control` **and** `lumascope_api` imports and constructs | Would drive the scope. **Every hardware verb is a stub.** |

The probe falls back to Observed on any doubt. The dashboard shows which mode
is live and, in Observed mode, replaces the per-job controls with the reason
rather than rendering a button that does nothing.

#### Stubbed pending headless control

These are wired end to end — API, job state, UI — but **do not touch the
microscope**. Each is a `TODO(headless)` in `backend/acquisition.py`:

- `ControlledAcquisition.start` — translating a job's plate/tiles/slices into
  a LumaViewPro protocol and running it.
- `.pause` / `.resume` — halting and resuming without losing stage position.
- `.cancel` — aborting and parking the stage safely.

Until those are confirmed against real hardware, start and stop the run in
LumaViewPro itself and add it here with **Track an acquisition run** so its
progress and ETA are followed. Everything else — segmentation queueing,
pause/resume/cancel, ETAs, telemetry — is real in both modes.

### Pause, resume, cancel

- **Per-job pause** finishes the batch in flight, then stops pulling work for
  that job. Files keep arriving and joining it, so the backlog visibly grows —
  that is the point, and the dashboard says so.
- **Global pause** (`POST /api/segmentation/pause`) holds the whole worker,
  independent of any one job. It is stored in SQLite, so it survives a
  restart; the log says so at startup.
- **Resume** picks up every file that arrived while paused, oldest first, with
  no gaps — pending files are ordered by discovery time, not by when the pause
  ended.
- **Cancel** on a running segmentation job stops after the current batch. Its
  remaining files stay `pending`, never `failed`. They are reported separately
  as `set_aside` and deliberately left out of `backlog`: nothing is going to
  segment them, and counting them would keep the banner red forever.

### ETAs

Segmentation ETA is a **range**, built from the per-image times of up to 20
recent batches (inter-quartile spread), not a single average. Below three
completed batches it reports `not enough data` and shows no number at all. A
fabricated ETA is worse than none on a dashboard someone is making decisions
from. Confidence is `high` only with six or more batches and a spread under
1.5x; otherwise `low`, shown as "(rough)".

Acquisition ETA is the expected total over the rate observed so far, widened
while little of the plate is in.

Queued jobs also show **estimated time until they start**, accumulated from
the upper bound of everything ahead. If any job ahead has no ETA, the ones
behind it show none either rather than a number built on a guess.

## Remote access

The server binds `--host` / `--port`, default `0.0.0.0:8000`.

### Auth

Read-only views need no token. Every state-changing POST requires
`X-Auth-Token` matching `MM_AUTH_TOKEN` (or `--auth-token`), compared with
`secrets.compare_digest`. There is no user system and there should not be one
for a lab instrument. Enter the token once in the dashboard's **Access** panel
and the browser remembers it.

If the token is unset, every POST is open and the app warns at startup and in
the UI.

### Tailscale, and a warning

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4          # e.g. 100.x.y.z
```

Then reach the dashboard at `http://100.x.y.z:8000` from any device on the
tailnet — phone included — with no port forwarding and no open inbound port.

> **Do not port-forward this machine to the public internet.** It is on the
> same host as the microscope control software, and with acquisition control
> enabled it can move hardware. A tailnet (or a VPN, or an SSH tunnel) keeps
> it off the public internet entirely. If you expose it anyway, set
> `MM_AUTH_TOKEN` first — but prefer not exposing it.

### Dropped connections

People close laptops. The dashboard reconnects on its own with exponential
backoff (1s doubling to 30s), and if no update arrives for 15 seconds it says
so in a calm banner rather than showing stale numbers as if they were live.

## Preflight

`preflight.py` checks the things that actually break on a fresh workstation
and exits non-zero if any of them would. Run it before a real acquisition,
not during one.

```bash
python preflight.py --input-dir /data/lumaviewpro/output --output-dir /data/masks
```

It checks: Python version; torch import and CUDA build; which device the app
will pick; **whether the installed torch wheel has kernels for this card**
(`sm_120` in `torch.cuda.get_arch_list()` — the RTX 5090 trap) plus a real
512x512 matmul to prove a kernel launches; `nvidia-smi` and its fields;
the `dinov3` import; cellpose; whether the cpdino weights are already cached;
the input dir (exists, readable, TIFF count), output dir writability, free
space, the input volume's filesystem; the port; and whether `frontend/dist`
was built.

Three optional flags go further, at the cost of a ~1.1 GB download on first use:

```bash
python preflight.py --input-dir ... --load-model   # actually construct cpdino
python preflight.py --input-dir ... --segment      # ...and segment one image
python preflight.py --input-dir ... --throughput   # ...and time a full-size batch
```

A fourth flag checks the capacity-study instrumentation instead, and needs a
**running station** rather than a model:

```bash
python preflight.py --study --station-token "$MM_ST_AUTH_TOKEN"
```

It sets a phase, changes the batch size live, waits for a telemetry sample
carrying both, confirms the inter-arrival, disk read/write and GPU fields are
really being populated on this machine, reads `/api/report` and
`/api/report.md`, and puts the phase and batch size back — including when a
check fails part-way. Because it writes to the station, it refuses to run
while a named run is acquiring (probing would relabel that run's samples)
unless `--force`. See `station/README.md`, "Capacity and tuning study".

Warnings are survivable; any `FAIL` means don't start the run.

### Will it keep up?

The `throughput` section answers that before the run rather than during it. The
plate plan defaults to the planned acquisition — 96 wells, 2x2 tiles, 20 slices,
2 channels, 2 hours = **15,360 images, one every 0.47 s** — and is overridable
with `--wells / --tiles / --slices / --channels / --run-hours`. The image size
is read from the first TIFF in the input dir, not assumed.

`--throughput` then times one real batch at that size (after a warm-up call, so
one-off costs don't skew it) and projects the backlog: headroom if segmentation
keeps up, or how many images are still queued when acquisition ends, how long
they take to clear, and how far the plan would have to shrink to hold backlog
flat. Falling behind is a `WARN`, not a `FAIL` — a growing backlog is a valid
thing to demonstrate, it just shouldn't be a surprise.

For reference, `neil-linlab-GamingPC` measures **0.86 s/image end to end at
1900x1900** (0.68 s of that GPU inference) with two images per GPU call, against
acquisition at about 1.65 images/s — so it runs roughly 2x behind during a
plate and drains afterwards. See `docs/WORKSTATION.md` for the full set.

## Local test

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
export MM_AUTH_TOKEN="$(openssl rand -hex 16)"   # required for pause/cancel/queue
python -m backend.main \
  --input-dir /data/lumaviewpro/output \
  --output-dir /data/masks \
  --db-path /var/lib/microscope-monitor/monitor.sqlite \
  --specs-path /var/lib/microscope-monitor/specs.txt \
  --port 8000
```

Then open the dashboard, paste that token into **Access** once, and add the
run with **Track an acquisition run** (plate name and expected image count —
96 wells x 2x2 tiles x 20 slices x 2 channels is 15,360).

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
| `--auth-token` | `MM_AUTH_TOKEN` | unset (POSTs open) |
| `--enable-acquisition-control` | `MM_ENABLE_ACQUISITION_CONTROL` | off (Observed mode) |

## API

| endpoint | returns |
|---|---|
| `GET /api/status` | current snapshot (counts, rates, ETA, GPU/CPU/RAM/disk read+write, inter-arrival, phase, worker state) |
| `GET /api/history?minutes=30` | time series of those snapshots, for the charts |
| `GET /api/specs` | machine spec dump as JSON + rendered text |
| `GET /api/specs.txt` | the same dump as plain text |
| `GET /api/failures` | failed files with error text |
| `GET /api/config` | resolved config, device, model |
| `GET /api/queue` | both queues: position, status, progress, `eta_seconds`, `eta_confidence`, adapter mode |
| `GET /api/jobs/{id}` | one job |
| `POST /api/jobs` | create an acquisition job (`plate`, `expected_images`, `notes`) |
| `POST /api/jobs/{id}/pause` | pause one job |
| `POST /api/jobs/{id}/resume` | resume one job |
| `POST /api/jobs/{id}/cancel` | cancel one job |
| `POST /api/segmentation/pause` | global worker pause (`{"paused": true}`) |
| `WS /ws` | a snapshot pushed every 2 s, queue state included |

The four `POST`s require `X-Auth-Token` when `MM_AUTH_TOKEN` is set; everything
else is open.

## Specs

On startup the app captures `uname -a`, `lscpu`, `free -h`, `lsblk`, `df -h`,
the full `nvidia-smi` plus the driver version, torch version / CUDA build /
device name / compute capability, the cellpose version, and which block device
holds the input directory (`findmnt -T`, plus `lsblk` for that device). It is
stored in SQLite, served at `/api/specs`, written to `specs.txt`, and the
dashboard shows it in a panel with a **Copy** button.

Off Linux the Linux-only commands report themselves unavailable and
`sysctl machdep.cpu.brand_string` / `system_profiler SPHardwareDataType` are
captured instead, so the dump still says which machine produced it. Only a dump
from `neil-linlab-GamingPC` describes the lab.
