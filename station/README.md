# Station

Monitor **and control** the microscope and the segmentation from anywhere,
including off campus, for unattended overnight runs.

This is a separate app from the dashboard in `../backend` + `../frontend`.
That one is file-centric and answers one question — does the backlog grow.
This one is organised around **runs**: a run is a plate, and every image and
mask belongs to one. Different port, different database, different frontend.
The dashboard is untouched and still works.

```
station/
  config.py       MM_ST_* env, port 8100
  db.py           runs, images, telemetry, events, kv
  wells.py        which plate well an image came from
  worker.py       segmentation, subclassing the dashboard's worker
  acquisition.py  ControlledAcquisition (stubbed) / ObservedAcquisition
  runs.py         the acquisition lock, pause/resume/cancel, ETAs, queue
  notify.py       webhooks with debounce
  telemetry.py    run-tagged samples
  app.py          FastAPI
  selftest.py     python -m station.selftest
  frontend/       React + Vite + recharts on Gentelella v4
```

> **Do not point both apps at the same `--input-dir` at once.** Each runs its
> own segmenter; they would both segment the same TIFFs.

## The partial-file guard is not duplicated

`StationWorker` subclasses `backend.worker.Worker`, so `scan()` — which holds
the guard (size identical across two consecutive polls **and** held for
`--stable-seconds`, plus the truncated-read retry) and the backlog accounting
— is inherited verbatim. Only the run-aware parts are overridden. A fix to
that logic lands in both apps at once, and the two cannot drift.

## Run it

```bash
source ../.venv/bin/activate
cd frontend && npm install && npm run build && cd ..
cd .. && python -m station.main \
  --input-dir /data/lumaviewpro/output \
  --output-dir /data/station-masks \
  --db-path /var/lib/microscope-monitor/station.sqlite
```

Open `http://<workstation-ip>:8100`. Add `--dry-run` to see the UI without a
working Cellpose install. For the frontend dev server, `npm run dev` in
`station/frontend` serves :5174 and proxies to :8100.

Self-test (no pytest needed):

```bash
python -m station.selftest
```

## Acquisition is conditional

Headless control of LumaViewPro is **unverified**. The app probes at startup
and picks one of two adapters:

| adapter | when | what it does |
|---|---|---|
| `ObservedAcquisition` | **the default** | No control. Progress is inferred from files appearing, against the expected total you enter. |
| `ControlledAcquisition` | `--enable-acquisition-control` **and** `lumascope_api` imports and constructs | Would drive the scope. **Every hardware verb is a stub.** |

The probe falls back to Observed on any doubt. In Observed mode the
acquisition control buttons are **not rendered at all** — not disabled — and
the row carries a one-line reason instead. The backend refuses those calls
with `409` regardless of what any client sends.

### Stubbed pending headless-control confirmation

Wired end to end — API, run state, UI — but they **do not move the
microscope**. Each is a `TODO(headless)` in `station/acquisition.py`:

- `ControlledAcquisition.start` — turning a run's plate, tile grid, z-stack
  and channels into a LumaViewPro protocol and running it.
- `.pause` / `.resume` — halting and resuming without losing stage position.
- `.cancel` — aborting and parking the stage safely.

Until confirmed on hardware, start and stop the run in LumaViewPro and press
**Track a run** here. Everything else — segmentation, queueing, pause,
resume, cancel, ETAs, the plate map, history, alerts — is real in both modes.

## Runs and the queue

One acquisition at a time. The lock is taken inside one mutex in
`RunManager.create`, not in the UI: two browsers pressing the button at the
same moment produce one run and one `409`.

Images arriving with no run active land in an `Unassigned` run rather than
being dropped — someone starting LumaViewPro before touching the dashboard is
a real case.

The queue shows both lanes with position and, for queued runs, estimated time
to start, accumulated from the upper bound of everything ahead. If anything
ahead has no ETA, the ones behind show none rather than a number built on a
guess.

### Plate map

96 wells (configurable via `--plate-rows` / `--plate-cols`), each in one
state: **not yet**, **acquired** (images present, none segmented),
**segmenting** (partly done), **segmented**, **failed**. Each state differs in
fill *and* border, so the map reads without colour; hovering gives the counts.

The well comes from the image path — any component or filename prefix that
looks like a well label, preferring the one nearest the file. No naming
scheme is hardcoded. Images whose path says nothing are counted as
`unplaced` and shown as such rather than guessed into A01.

## Pause, resume, cancel

- **Per-run segmentation pause** finishes the batch in flight, then stops
  pulling work for that run. Images keep arriving and joining it, so the
  backlog visibly grows — the UI says so.
- **Global pause** holds the whole worker. It lives in SQLite and survives a
  restart; the log says so at startup.
- **Resume** takes everything that arrived while paused, oldest first, no
  gaps: pending images are ordered by discovery time.
- **Cancel** stops after the current batch. Remaining images stay `pending`,
  never `failed`, and are reported as `set_aside` rather than counted in the
  backlog — nothing is going to segment them, and counting them would hold
  the warning red forever.

## ETAs

Segmentation ETA is a **range**, from the inter-quartile spread of up to 20
recent batches. Below three completed batches it reports
`not enough data` and shows no number. Confidence is `high` only with six or
more batches and a spread under 1.5x, otherwise `low`, shown as "(rough)".

Acquisition ETA is the expected total over the rate observed so far, widened
while little of the plate is in.

## Run history

`GET /api/runs` lists every run with summary stats; the **Run history** page
is that table. Clicking a run opens its detail page, which replays that run's
charts from stored telemetry — samples are tagged with their run id, so a
finished run's backlog and throughput can be compared against last week's
months later.

## Alerts

Set `MM_ST_WEBHOOK_URL` to a Slack incoming webhook (the payload is
`{"text": ...}`, which most other receivers also accept). Four triggers:

| trigger | when |
|---|---|
| `run_complete` | a run reaches `done` |
| `run_failed` | a run reaches `failed` |
| `falling_behind` | the backlog has been growing continuously for `--falling-behind-seconds` (default 300) |
| `disk_low` | free space drops below `--disk-free-threshold-gb` (default 50) |

Two layers of debounce, because a 3am alert is only useful if it is rare
enough to be believed:

- **sustained** — falling-behind must hold continuously for the whole window
  before it fires at all, so one slow z-stack says nothing.
- **cooldown** — the same kind cannot fire again within
  `--webhook-min-interval` (default 900s). Tracked in the `events` table, so a
  restart does not reset it and re-spam.

Run-complete and run-failed are keyed per run and simply never repeat.

Every alert is recorded whether or not delivery succeeded — an alert that
could not be sent still happened, and the **Alerts** panel shows it with
`delivered = no` rather than pretending the night was quiet. **Send test**
checks the webhook end to end.

## Remote access

Bind with `--host` / `--port`, default `0.0.0.0:8100`.

### Auth

Every `GET` is open — read-only monitoring is for the whole lab. Every
state-changing `POST` requires `X-Auth-Token` matching `MM_ST_AUTH_TOKEN`,
compared with `secrets.compare_digest`. There is no user system and there
should not be one for a lab instrument. Enter the token once in the **Access**
panel; the browser remembers it, under its own key so it is not shared with
the other app.

With no token set, every POST is open and the app warns at startup and in the
UI.

### Tailscale — the recommended off-campus path

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4          # e.g. 100.x.y.z
```

Reach `http://100.x.y.z:8100` from any device on the tailnet — phone
included — with no port forwarding and no inbound port open to the internet.

> **Never port-forward this machine to the public internet.** It sits on the
> same host as the microscope control software, and with acquisition control
> enabled it can move hardware. A tailnet (or a VPN, or `ssh -L`) keeps it off
> the public internet entirely. If you expose it anyway, set
> `MM_ST_AUTH_TOKEN` first — but prefer not exposing it.

### Dropped connections

The page reconnects on its own with exponential backoff (1s doubling to 30s)
and shows a calm stale-data banner if no update arrives for 15 seconds,
rather than presenting stale numbers as live. `index.html` is served
`no-store` so a tab left open across a redeploy does not keep running the
previous bundle.

### On a phone

Below 600px the panels, plate map and queue stack to one column and the
charts collapse to sparklines. The two headline figures — images captured and
images waiting — stay large enough to read without zooming.

## As a service

```bash
sudo mkdir -p /var/lib/microscope-monitor && sudo chown lab:lab /var/lib/microscope-monitor
sudo cp ../deploy/microscope-station.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now microscope-station
journalctl -u microscope-station -f
```

The token and the webhook URL do not belong in the unit file. The unit reads
them from a file only the service user can read:

```bash
sudo install -m 600 -o lab -g lab /dev/null /etc/microscope-station.env
sudo tee /etc/microscope-station.env >/dev/null <<'ENV'
MM_ST_AUTH_TOKEN=...
MM_ST_WEBHOOK_URL=https://hooks.slack.com/services/...
ENV
sudo systemctl restart microscope-station
```

`EnvironmentFile=-` means the leading dash makes the file optional: the
service still starts without it and logs that no token and no webhook are
set.

`ExecStartPre` runs `preflight.py` against this service's own input dir,
output dir and port, so a missing GPU stack or an unreadable input directory
stops the service rather than producing an hour of silent failures. Its
"frontend built" check looks at the dashboard's `frontend/dist`, so expect
that one to warn here; warnings do not block startup.

### Running it alongside microscope-monitor

Both services run their own Cellpose worker. Pointed at the same input
directory they will both segment the same TIFFs — two models on one GPU,
double the VRAM, two sets of masks. They are safe together **only** if
`MM_ST_INPUT_DIR` and `MM_INPUT_DIR` differ.

Systemd cannot catch this: the conflict is over a directory, not a port, and
nothing can know whether two paths resolve to the same files. To run the
station alone, which is the usual choice:

```bash
sudo systemctl disable --now microscope-monitor
```

## Config

| flag | env | default |
|---|---|---|
| `--input-dir` | `MM_ST_INPUT_DIR` | `./data/station/input` |
| `--output-dir` | `MM_ST_OUTPUT_DIR` | `./data/station/masks` |
| `--db-path` | `MM_ST_DB_PATH` | `./data/station.sqlite` |
| `--host` / `--port` | `MM_ST_HOST` / `MM_ST_PORT` | `0.0.0.0` / `8100` |
| `--batch-size` | `MM_ST_BATCH_SIZE` | `16` |
| `--poll-interval` | `MM_ST_POLL_INTERVAL` | `5` |
| `--stable-seconds` | `MM_ST_STABLE_SECONDS` | `0` = 2x poll |
| `--telemetry-interval` | `MM_ST_TELEMETRY_INTERVAL` | `2` |
| `--model` | `MM_ST_MODEL` | `cpdino` |
| `--dry-run` | `MM_ST_DRY_RUN` | off |
| `--auth-token` | `MM_ST_AUTH_TOKEN` | unset (POSTs open) |
| `--enable-acquisition-control` | `MM_ST_ENABLE_ACQUISITION_CONTROL` | off |
| `--webhook-url` | `MM_ST_WEBHOOK_URL` | unset (nobody is alerted) |
| `--webhook-min-interval` | `MM_ST_WEBHOOK_MIN_INTERVAL` | `900` |
| `--falling-behind-seconds` | `MM_ST_FALLING_BEHIND_SECONDS` | `300` |
| `--disk-free-threshold-gb` | `MM_ST_DISK_FREE_GB` | `50` |
| `--plate-rows` / `--plate-cols` | `MM_ST_PLATE_ROWS` / `MM_ST_PLATE_COLS` | `8` / `12` |

## API

Read-only, no token:

| endpoint | returns |
|---|---|
| `GET /api/status` | live snapshot, queue included |
| `GET /api/queue` | both lanes with position, progress, ETA, adapter mode |
| `GET /api/runs` | every run with summary stats |
| `GET /api/runs/{id}` | one run, its plate map and its failures |
| `GET /api/runs/{id}/history` | stored telemetry for replay |
| `GET /api/runs/{id}/plate` | just the plate map |
| `GET /api/history?minutes=` | recent telemetry across runs |
| `GET /api/events` | alert history |
| `GET /api/failures`, `/api/specs`, `/api/config` | as the dashboard |
| `WS /ws` | a snapshot every 2s |

Token required (`X-Auth-Token`):

| endpoint | does |
|---|---|
| `POST /api/runs` | create a run (`name`, `plate`, `expected_image_count`, `notes`) |
| `POST /api/runs/{id}/pause\|resume\|cancel` | run control |
| `POST /api/runs/{id}/segmentation/pause` | pause one run's segmentation |
| `POST /api/segmentation/pause` | global worker pause |
| `POST /api/notify/test` | send a test alert |
