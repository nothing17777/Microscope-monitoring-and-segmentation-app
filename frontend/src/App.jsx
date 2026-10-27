import React, { useEffect, useMemo, useState } from 'react'
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

const SECTIONS = [
  ['overview', 'Overview'],
  ['throughput', 'Throughput'],
  ['resources', 'Resources'],
  ['machine-spec', 'Machine spec'],
  ['failures', 'Failures'],
]

// Chart strokes are read from the live AdminLTE/Bootstrap theme rather than
// hardcoded, so they stay legible if the theme is switched to light.
function themeColor(name, fallback) {
  if (typeof window === 'undefined') return fallback
  const cs = getComputedStyle(document.documentElement)
  return (
    cs.getPropertyValue(`--bs-${name}-text-emphasis`).trim() ||
    cs.getPropertyValue(`--bs-${name}`).trim() ||
    fallback
  )
}

// --- formatting -------------------------------------------------------------

const nf = (n, d = 0) =>
  n === null || n === undefined || Number.isNaN(n) ? '—' : Number(n).toFixed(d)

function bytes(b) {
  if (b === null || b === undefined) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, v = Number(b)
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`
}

function dur(s) {
  if (s === null || s === undefined || !Number.isFinite(s)) return '—'
  s = Math.max(0, Math.round(s))
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60
  return h ? `${h}h ${String(m).padStart(2, '0')}m`
    : m ? `${m}m ${String(sec).padStart(2, '0')}s`
      : `${sec}s`
}

const clock = (ts) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

// --- data -------------------------------------------------------------------

function useSnapshot() {
  const [snap, setSnap] = useState(null)
  const [live, setLive] = useState(false)

  useEffect(() => {
    let closed = false
    let retry
    let sock
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      sock = new WebSocket(`${proto}://${location.host}/ws`)
      sock.onopen = () => setLive(true)
      sock.onmessage = (e) => setSnap(JSON.parse(e.data))
      sock.onclose = () => {
        setLive(false)
        if (!closed) retry = setTimeout(connect, 2000)
      }
      sock.onerror = () => sock.close()
    }
    connect()
    return () => { closed = true; clearTimeout(retry); sock?.close() }
  }, [])

  // Populate the tiles before the first WebSocket frame lands.
  useEffect(() => {
    if (snap) return
    fetch('/api/status').then((r) => r.json()).then(setSnap).catch(() => {})
  }, [snap])

  return { snap, live }
}

function useJson(url, intervalMs) {
  const [data, setData] = useState(null)
  useEffect(() => {
    let alive = true
    const go = () =>
      fetch(url).then((r) => r.json()).then((d) => alive && setData(d)).catch(() => {})
    go()
    if (!intervalMs) return () => { alive = false }
    const id = setInterval(go, intervalMs)
    return () => { alive = false; clearInterval(id) }
  }, [url, intervalMs])
  return data
}

// --- building blocks --------------------------------------------------------

function SmallBox({ variant, value, unit, label, sub, icon }) {
  return (
    <div className="col-lg-3 col-sm-6 col-12">
      <div className={`small-box text-bg-${variant}`}>
        <div className="inner">
          <h3>
            {value}
            {unit ? <sup className="fs-5 ms-1">{unit}</sup> : null}
          </h3>
          <p>
            {label}
            {sub ? <small>{sub}</small> : null}
          </p>
        </div>
        <svg className="small-box-icon" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
          {icon}
        </svg>
      </div>
    </div>
  )
}

const ICONS = {
  check: <><path d="M4 12.5 9.5 18 20 6" /></>,
  stack: <><path d="M3 7.5 12 3l9 4.5-9 4.5-9-4.5Z" /><path d="M3 12l9 4.5L21 12" />
    <path d="M3 16.5 12 21l9-4.5" /></>,
  speed: <><circle cx="12" cy="13" r="8" /><path d="M12 13l4.5-4" /><path d="M12 2v2" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3.5 2" /></>,
  hourglass: <><path d="M7 3h10M7 21h10" /><path d="M7 3c0 5 5 6 5 9s-5 4-5 9" />
    <path d="M17 3c0 5-5 6-5 9s5 4 5 9" /></>,
  disk: <><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="3" /></>,
  warn: <><path d="M12 3 2 20h20L12 3Z" /><path d="M12 10v5" /><path d="M12 17.5v.5" /></>,
  chip: <><rect x="6" y="6" width="12" height="12" rx="2" /><path d="M9 3v3M15 3v3M9 18v3M15 18v3" />
    <path d="M3 9h3M3 15h3M18 9h3M18 15h3" /></>,
}

function ChartCard({ title, id, tools, data, series, yProps = {} }) {
  return (
    <div className="col-xl-6 col-12">
      <div className="card mb-4" id={id}>
        <div className="card-header d-flex align-items-center">
          <h3 className="card-title mb-0">{title}</h3>
          {tools ? <div className="card-tools ms-auto">{tools}</div> : null}
        </div>
        <div className="card-body">
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="var(--bs-border-color)" vertical={false} />
              <XAxis dataKey="t" stroke="var(--bs-secondary-color)"
                     tick={{ fontSize: 13 }} minTickGap={48} />
              <YAxis stroke="var(--bs-secondary-color)" tick={{ fontSize: 13 }}
                     width={58} {...yProps} />
              <Tooltip
                contentStyle={{
                  background: 'var(--bs-body-bg)',
                  border: '1px solid var(--bs-border-color)',
                  borderRadius: 'var(--bs-border-radius)',
                  fontSize: 14,
                }}
                labelStyle={{ color: 'var(--bs-secondary-color)' }}
              />
              <Legend wrapperStyle={{ fontSize: 14 }} />
              {series.map((s) => (
                <Line key={s.key} type="monotone" dataKey={s.key} name={s.name}
                      stroke={s.color} strokeWidth={2.5} dot={false}
                      isAnimationActive={false} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  )
}

function SpecCard() {
  const specs = useJson('/api/specs', 0)
  const [copied, setCopied] = useState(false)
  const text = specs?.text || ''

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      ta.remove()
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }

  return (
    <div className="col-xl-6 col-12">
      <div className="card mb-4" id="machine-spec">
        <div className="card-header d-flex align-items-center">
          <h3 className="card-title mb-0">Machine spec</h3>
          <div className="card-tools ms-auto d-flex gap-2">
            <button type="button" className="btn btn-sm btn-primary" onClick={copy}>
              {copied ? 'Copied' : 'Copy'}
            </button>
            <a className="btn btn-sm btn-outline-secondary" href="/api/specs.txt"
               target="_blank" rel="noreferrer">specs.txt</a>
          </div>
        </div>
        <div className="card-body">
          <pre className="spec-dump text-body-secondary mb-0">{text || 'capturing…'}</pre>
        </div>
      </div>
    </div>
  )
}

function FailuresCard() {
  const data = useJson('/api/failures', 10000)
  const rows = data?.failures || []
  return (
    <div className="col-xl-6 col-12">
      <div className="card mb-4" id="failures">
        <div className="card-header d-flex align-items-center">
          <h3 className="card-title mb-0">Failed files</h3>
          <div className="card-tools ms-auto">
            <span className={`badge ${rows.length ? 'text-bg-danger' : 'text-bg-secondary'}`}>
              {rows.length}
            </span>
          </div>
        </div>
        <div className="card-body p-0">
          {rows.length === 0 ? (
            <p className="text-body-secondary px-3 py-3 mb-0">No failures.</p>
          ) : (
            <div style={{ maxHeight: 420, overflow: 'auto' }}>
              <table className="table table-sm table-striped table-failures mb-0">
                <thead className="sticky-top">
                  <tr><th>When</th><th>File</th><th>Error</th></tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.path}>
                      <td className="text-nowrap">{r.done_at ? clock(r.done_at) : '—'}</td>
                      <td style={{ wordBreak: 'break-all' }}>{r.path}</td>
                      <td className="error text-danger-emphasis">{r.error}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// --- page -------------------------------------------------------------------

export default function App() {
  const { snap, live } = useSnapshot()
  const cfg = useJson('/api/config', 0)
  const [histMin, setHistMin] = useState(60)
  const hist = useJson(`/api/history?minutes=${histMin}`, 5000)

  const C = useMemo(() => ({
    found: themeColor('info', '#6edff6'),
    segmented: themeColor('success', '#75b798'),
    backlog: themeColor('warning', '#ffda6a'),
    failed: themeColor('danger', '#ea868f'),
    gpu: themeColor('primary', '#6ea8fe'),
    cpu: themeColor('success', '#75b798'),
    ram: themeColor('warning', '#ffda6a'),
    vram: themeColor('info', '#6edff6'),
  }), [])

  const series = useMemo(() => {
    const s = hist?.samples || []
    return s.map((x) => ({
      t: clock(x.ts),
      found: x.found,
      segmented: x.segmented,
      backlog: x.backlog,
      failed: x.failed,
      gpu: x.gpu?.gpu_util_pct ?? null,
      vram: x.gpu?.vram_used_mb != null ? x.gpu.vram_used_mb / 1024 : null,
      cpu: x.cpu_pct,
      ram: x.ram_total_bytes ? (100 * x.ram_used_bytes) / x.ram_total_bytes : null,
    }))
  }, [hist])

  const behind = !!snap?.falling_behind
  const gpu = snap?.gpu
  const pct = snap?.found ? (100 * snap.segmented) / snap.found : null

  return (
    <div className="app-wrapper">
      <nav className="app-header navbar navbar-expand bg-body">
        <div className="container-fluid">
          <ul className="navbar-nav">
            <li className="nav-item">
              <a className="nav-link" data-lte-toggle="sidebar" href="#" role="button"
                 aria-label="Toggle sidebar">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
                     stroke="currentColor" strokeWidth="2" aria-hidden="true">
                  <path d="M3 6h18M3 12h18M3 18h18" />
                </svg>
              </a>
            </li>
          </ul>
          <ul className="navbar-nav ms-auto align-items-center gap-2">
            <li className="nav-item">
              <span className={`badge ${live ? 'text-bg-success' : 'text-bg-danger'}`}>
                {live ? 'LIVE' : 'RECONNECTING'}
              </span>
            </li>
            <li className="nav-item">
              <span className="badge text-bg-secondary">
                device <strong>{cfg?.device?.toUpperCase() || '—'}</strong>
              </span>
            </li>
            <li className="nav-item">
              <span className="badge text-bg-secondary">
                model <strong>{cfg?.model || '—'}</strong>
              </span>
            </li>
            <li className="nav-item">
              <span className="badge text-bg-secondary">
                batch <strong>{snap?.worker?.batch_size ?? cfg?.batch_size ?? '—'}</strong>
              </span>
            </li>
            {snap?.worker?.oom_events ? (
              <li className="nav-item">
                <span className="badge text-bg-warning">
                  OOM retries <strong>{snap.worker.oom_events}</strong>
                </span>
              </li>
            ) : null}
          </ul>
        </div>
      </nav>

      <aside className="app-sidebar bg-body-secondary shadow">
        <div className="sidebar-brand">
          <a href="#overview" className="brand-link">
            <svg className="brand-image opacity-75" width="28" height="28" viewBox="0 0 24 24"
                 fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <circle cx="12" cy="12" r="3.2" />
              <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
              <path d="M5 5l2.2 2.2M16.8 16.8 19 19M19 5l-2.2 2.2M7.2 16.8 5 19" />
            </svg>
            <span className="brand-text fw-light">Microscope Monitor</span>
          </a>
        </div>
        <div className="sidebar-wrapper">
          <nav className="mt-2">
            <ul className="nav sidebar-menu flex-column" role="menu">
              {SECTIONS.map(([id, label]) => (
                <li className="nav-item" key={id}>
                  <a href={`#${id}`} className="nav-link">
                    <p>{label}</p>
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </aside>

      <main className="app-main">
        <div className="app-content-header">
          <div className="container-fluid">
            <div className="row align-items-center">
              <div className="col-sm-6">
                <h3 className="mb-0">Acquisition &amp; segmentation</h3>
              </div>
              <div className="col-sm-6 text-sm-end small text-body-secondary">
                <span className="text-truncate d-inline-block" style={{ maxWidth: '100%' }}>
                  input {cfg?.input_dir || '—'}
                </span>
              </div>
            </div>
          </div>
        </div>

        <div className="app-content">
          <div className="container-fluid" id="overview">

            <div
              id="backlog-banner"
              className={`card mb-4 border-2 ${behind
                ? 'border-danger falling-behind' : 'border-success'}`}
            >
              <div className="card-body d-flex flex-wrap align-items-center gap-4">
                <div className="text-center">
                  <div className="text-uppercase text-body-secondary small fw-semibold">
                    Backlog
                  </div>
                  <div className={`backlog-number ${behind ? 'text-danger' : 'text-success'}`}>
                    {snap ? snap.backlog : '—'}
                  </div>
                </div>
                <div className="flex-grow-1">
                  <div className={`verdict ${behind ? 'text-danger' : 'text-success'}`}>
                    {behind ? 'SEGMENTATION FALLING BEHIND' : 'KEEPING UP'}
                  </div>
                  <div className="text-body-secondary mt-1">
                    backlog trend over last 5 min:{' '}
                    <strong>
                      {snap
                        ? `${snap.backlog_trend_per_min > 0 ? '+' : ''}${nf(snap.backlog_trend_per_min, 1)} images/min`
                        : '—'}
                    </strong>
                    {' · '}segmenting {nf(snap?.current_rate, 2)} img/s
                    {' vs '}{nf(snap?.avg_rate, 2)} img/s average
                  </div>
                </div>
              </div>
            </div>

            <div className="row">
              <SmallBox
                variant="success" icon={ICONS.check}
                value={snap ? snap.segmented : '—'}
                unit={snap ? `/ ${snap.found}` : null}
                label="Segmented / found"
                sub={pct === null ? null : `${nf(pct, 1)}% complete`}
              />
              <SmallBox
                variant={behind ? 'danger' : 'warning'} icon={ICONS.stack}
                value={snap ? snap.backlog : '—'}
                label="Backlog" sub="pending, size-stable files"
              />
              <SmallBox
                variant="primary" icon={ICONS.speed}
                value={nf(snap?.current_rate, 2)} unit="img/s"
                label="Current rate" sub={`${nf(snap?.avg_rate, 2)} img/s average`}
              />
              <SmallBox
                variant="secondary" icon={ICONS.clock}
                value={dur(snap?.acquisition_elapsed_s)}
                label="Elapsed" sub={`app up ${dur(snap?.elapsed_s)}`}
              />
              <SmallBox
                variant="secondary" icon={ICONS.hourglass}
                value={dur(snap?.eta_s)}
                label="ETA to drain" sub="at the current rate"
              />
              <SmallBox
                variant="info" icon={ICONS.disk}
                value={bytes(snap?.disk_free_bytes)}
                label="Disk free"
                sub={`${bytes(snap?.input_bytes)} written to input dir`}
              />
              <SmallBox
                variant={snap?.failed ? 'danger' : 'secondary'} icon={ICONS.warn}
                value={snap ? snap.failed : '—'}
                label="Failed" sub="listed below"
              />
              <SmallBox
                variant="primary" icon={ICONS.chip}
                value={gpu ? nf(gpu.gpu_util_pct) : 'n/a'} unit={gpu ? '%' : null}
                label="GPU"
                sub={gpu
                  ? `${nf(gpu.vram_used_mb / 1024, 1)} / ${nf(gpu.vram_total_mb / 1024, 1)} GB · ${nf(gpu.gpu_temp_c)}°C · ${nf(gpu.gpu_power_w)} W`
                  : 'no nvidia-smi on this machine'}
              />
            </div>

            <div className="row" id="throughput">
              <ChartCard
                title="Images found vs segmented" data={series}
                tools={
                  <select
                    className="form-select form-select-sm"
                    value={histMin}
                    onChange={(e) => setHistMin(Number(e.target.value))}
                    aria-label="History window"
                  >
                    {[5, 15, 30, 60, 180].map((m) => (
                      <option key={m} value={m}>last {m} min</option>
                    ))}
                  </select>
                }
                series={[
                  { key: 'found', name: 'found', color: C.found },
                  { key: 'segmented', name: 'segmented', color: C.segmented },
                ]}
              />
              <ChartCard
                title="Backlog" data={series}
                series={[
                  { key: 'backlog', name: 'backlog', color: C.backlog },
                  { key: 'failed', name: 'failed', color: C.failed },
                ]}
              />
            </div>

            <div className="row" id="resources">
              <ChartCard
                title="GPU utilisation (%) and VRAM (GB)" data={series}
                series={[
                  { key: 'gpu', name: 'GPU util %', color: C.gpu },
                  { key: 'vram', name: 'VRAM GB', color: C.vram },
                ]}
              />
              <ChartCard
                title="CPU (%) and RAM (%)" data={series} yProps={{ domain: [0, 100] }}
                series={[
                  { key: 'cpu', name: 'CPU %', color: C.cpu },
                  { key: 'ram', name: 'RAM %', color: C.ram },
                ]}
              />
            </div>

            <div className="row">
              <SpecCard />
              <FailuresCard />
            </div>

          </div>
        </div>
      </main>
    </div>
  )
}
