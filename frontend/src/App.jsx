import React, { useEffect, useMemo, useRef, useState } from 'react'
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import Queue from './Queue'
import { getToken, setToken } from './api'
import { usePoll, useLive } from './useLive'
import { bytes, clock, dur, durRange, int, nf, rate } from './format'

const SECTIONS = [
  ['verdict', 'Status'],
  ['queues', 'Queues'],
  ['charts', 'Charts'],
  ['resources', 'Resources'],
  ['machine', 'Machine'],
  ['failures', 'Failures'],
]

// Chart strokes come from the live theme, so they stay correct in both modes.
function token(name, fallback) {
  if (typeof window === 'undefined') return fallback
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

// The state colours are declared on the .state-* classes, not on :root, so
// they have to be looked up by name here. The backlog line must agree with
// the verdict: a green line above a red headline is worse than no colour.
const STATE_TOKEN = {
  idle: '--text-muted',
  'keeping-up': '--primary',
  slipping: '--yellow',
  behind: '--red',
}
const stateColor = (key) => token(STATE_TOKEN[key] || '--primary', '#1ABB9C')

// --- theme ------------------------------------------------------------------

function useTheme() {
  const [theme, setTheme] = useState(
    () => document.documentElement.getAttribute('data-theme') || 'dark',
  )
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try { localStorage.setItem('mm_theme', theme) } catch { /* private mode */ }
  }, [theme])
  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))]
}

// --- verdict ----------------------------------------------------------------

// The single question the dashboard exists to answer. Three states, each
// named in words as well as coloured, so the page reads in grayscale.
function verdictOf(snap) {
  if (!snap) return { key: 'idle', cls: 'state-idle', headline: 'Connecting…', sub: '' }
  const { backlog, falling_behind: behind, backlog_trend_per_min: trend, found } = snap
  if (!found) {
    return {
      key: 'idle', cls: 'state-idle', headline: 'Waiting for images',
      sub: 'Nothing has landed in the input folder yet. The run has not started, or LumaViewPro is writing somewhere else.',
    }
  }
  if (behind) {
    return {
      key: 'behind', cls: 'state-bad', headline: 'Falling behind',
      sub: `Backlog is growing by about ${nf(trend, 0)} images a minute. Segmentation cannot keep up with acquisition at this rate.`,
    }
  }
  if (backlog > 0 && trend > 0) {
    return {
      key: 'slipping', cls: 'state-warn', headline: 'Slipping',
      sub: 'The backlog is rising slowly. Watch it — it has not yet crossed into falling behind.',
    }
  }
  return {
    key: 'keeping-up', cls: 'state-ok', headline: 'Keeping up',
    sub: 'Segmentation is clearing images at least as fast as they arrive.',
  }
}

// Rank, so the flash fires only when things get worse. A run that wobbles
// across the slipping threshold would otherwise blink all afternoon.
const SEVERITY = { idle: 0, 'keeping-up': 0, slipping: 1, behind: 2 }

function Verdict({ snap }) {
  const v = verdictOf(snap)
  const prev = useRef(v.key)
  const [flash, setFlash] = useState(false)
  useEffect(() => {
    const worse = SEVERITY[v.key] > SEVERITY[prev.current]
    prev.current = v.key
    if (!worse) return undefined
    setFlash(true)
    const t = setTimeout(() => setFlash(false), 1300)
    return () => clearTimeout(t)
  }, [v.key])

  return (
    <section id="verdict" className={`verdict ${v.cls} ${flash ? 'just-changed' : ''}`}>
      <div>
        <div className="verdict-headline">{v.headline}</div>
        <p className="verdict-sub">{v.sub}</p>
      </div>
      <div className="verdict-figure">
        <span className="verdict-number metric-value" style={{ color: 'var(--state)' }}>
          {int(snap?.backlog ?? 0)}
        </span>
        <span className="verdict-unit">
          {snap?.backlog === 1 ? 'image waiting' : 'images waiting'}
        </span>
      </div>
    </section>
  )
}

// --- tiles ------------------------------------------------------------------

function Metric({ label, value, unit, sub, state }) {
  return (
    <div className={`metric ${state ? `is-state ${state}` : ''}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">
        {value}{unit ? <span className="unit">{unit}</span> : null}
      </div>
      <div className="metric-sub">{sub}</div>
    </div>
  )
}

// --- charts -----------------------------------------------------------------

function Chart({ data, lines, hero, yLabel }) {
  const grid = token('--border-color', '#333')
  const muted = token('--text-muted', '#888')
  const surface = token('--bg-surface', '#1a2332')
  return (
    <div className={hero ? 'chart-box-hero' : 'chart-box'}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -12 }}>
          <CartesianGrid stroke={grid} strokeDasharray="2 4" vertical={false} />
          <XAxis dataKey="t" tick={{ fill: muted, fontSize: 11 }} stroke={grid}
                 tickLine={false} minTickGap={48} />
          <YAxis tick={{ fill: muted, fontSize: 11 }} stroke={grid} tickLine={false}
                 width={46} allowDecimals={false}
                 label={yLabel ? { value: yLabel, angle: -90, position: 'insideLeft',
                                   fill: muted, fontSize: 11 } : undefined} />
          <Tooltip
            contentStyle={{ background: surface, border: `1px solid ${grid}`,
                            borderRadius: 6, fontSize: 12 }}
            labelStyle={{ color: muted }} />
          {lines.map((l) => (
            // Thin, unfilled, directly labelled. No legend, no dots.
            <Line key={l.key} type="monotone" dataKey={l.key} name={l.name}
                  stroke={l.color} strokeWidth={1.75} dot={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function Card({ id, title, aside, children, note }) {
  return (
    <section className="card" id={id}>
      <div className="card-header">
        <h3 className="card-title">{title}</h3>
        {aside}
      </div>
      <div className="card-body">
        {children}
        {note ? <p className="chart-note" style={{ marginTop: 8 }}>{note}</p> : null}
      </div>
    </section>
  )
}

// --- shell ------------------------------------------------------------------

function TokenBox({ required }) {
  const [value, setValue] = useState(getToken())
  const [saved, setSaved] = useState(false)
  if (!required) return null
  return (
    <form
      className="inline-form"
      onSubmit={(e) => { e.preventDefault(); setToken(value); setSaved(true); setTimeout(() => setSaved(false), 2000) }}
    >
      <div className="field" style={{ flex: '1 1 12rem' }}>
        <label htmlFor="tok">Access token</label>
        <input id="tok" type="password" value={value} autoComplete="off"
               placeholder="required to pause, cancel or queue"
               onChange={(e) => setValue(e.target.value)} />
      </div>
      <button className="btn btn-primary">{saved ? 'Saved' : 'Save'}</button>
    </form>
  )
}

export default function App() {
  const { snap, connected, stale, refresh } = useLive()
  const [theme, toggleTheme] = useTheme()
  const [navOpen, setNavOpen] = useState(false)
  const history = usePoll('/api/history?minutes=30', 10000)
  const config = usePoll('/api/config', 60000)
  const specs = usePoll('/api/specs', 0)
  const failures = usePoll('/api/failures', 15000)
  const [queueNonce, setQueueNonce] = useState(0)
  const queue = snap?.queue

  // Re-render charts when the theme flips so stroke colours follow.
  const [, force] = useState(0)
  useEffect(() => { force((n) => n + 1) }, [theme])

  const series = useMemo(() => {
    const s = history?.samples ?? []
    return s.map((d) => ({
      t: clock(d.ts),
      backlog: d.backlog,
      segmented: d.segmented,
      found: d.found,
      rate: Number((d.current_rate ?? 0).toFixed(3)),
      cpu: d.cpu_pct,
      ram: d.ram_pct,
      gpu: d.gpu?.gpu_util_pct ?? null,
      vram: d.gpu?.vram_used_mb ?? null,
    }))
  }, [history])

  const v = verdictOf(snap)
  const eta = snap?.eta_confidence && snap.eta_confidence !== 'none'
    ? durRange(snap.eta_low, snap.eta_high)
    : null

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to content</a>

      <aside className={`sidebar ${navOpen ? 'open' : ''}`}>
        <div className="sidebar-brand">
          <span className="brand-name">Microscope Monitor</span>
        </div>
        <nav className="sidebar-nav">
          <div className="nav-group">
            <div className="nav-label">Dashboard</div>
            {SECTIONS.map(([id, label]) => (
              <a key={id} className="nav-link" href={`#${id}`} onClick={() => setNavOpen(false)}>
                {label}
              </a>
            ))}
          </div>
        </nav>
        <div className="sidebar-footer">
          <div className="sidebar-user">
            <div className="sidebar-user-info">
              <div className="name">{config?.device ?? '—'}</div>
              <div className="role">{config?.model ?? '—'}</div>
            </div>
          </div>
        </div>
      </aside>
      <div className="sidebar-backdrop" hidden={!navOpen} onClick={() => setNavOpen(false)} />

      <header className="topbar">
        <div className="topbar-left">
          <button className="sidebar-toggle" onClick={() => setNavOpen((o) => !o)}
                  aria-label="Toggle navigation">☰</button>
          <nav className="breadcrumb">
            <span className="current">{v.headline}</span>
          </nav>
        </div>
        <div className="topbar-right">
          <span className="muted" style={{ fontSize: 12 }}>
            {connected ? 'live' : 'reconnecting'}
          </span>
          {/* Gentelella's .theme-toggle rules show only the icon for the state
              the click leads to: a sun while dark, a moon while light. */}
          <button className="btn btn-ghost btn-sm theme-toggle" onClick={toggleTheme}
                  aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>
            <svg className="theme-icon-light" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
                 aria-hidden="true">
              <circle cx="12" cy="12" r="4.2" />
              <path d="M12 2.6v2.1M12 19.3v2.1M4.2 4.2l1.5 1.5M18.3 18.3l1.5 1.5M2.6 12h2.1M19.3 12h2.1M4.2 19.8l1.5-1.5M18.3 5.7l1.5-1.5" />
            </svg>
            <svg className="theme-icon-dark" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
                 strokeLinejoin="round" aria-hidden="true">
              <path d="M20.5 14.4A8.5 8.5 0 1 1 9.6 3.5a6.8 6.8 0 0 0 10.9 10.9Z" />
            </svg>
            <span>{theme === 'dark' ? 'Light' : 'Dark'}</span>
          </button>
        </div>
      </header>

      <main className="main" id="main-content">
        <div className="page-wrapper stack">
          <div className="page-header page-header-row">
            <div>
              <div className="page-pretitle">
                {config?.dry_run ? 'DRY RUN — masks are a threshold, not Cellpose' : 'Live run'}
              </div>
              <h1 className="page-title">Acquisition &amp; segmentation</h1>
            </div>
          </div>

          {stale ? (
            <div className="notice state-warn">
              <strong>Not updating.</strong>
              No data for more than 15 seconds — the numbers below are the last ones received.
              {connected ? ' The connection is open but quiet.' : ' Reconnecting…'}
              <button className="btn btn-ghost btn-sm" onClick={refresh}>Retry now</button>
            </div>
          ) : null}
          {!connected && !stale ? (
            <div className="notice state-idle">
              <strong>Reconnecting.</strong> The live feed dropped; retrying with backoff.
            </div>
          ) : null}
          {queue?.global_pause ? (
            <div className="notice state-warn">
              <strong>Segmentation is paused.</strong> Files are still being discovered and the
              backlog is growing. Resume the worker in the Segmentation panel.
            </div>
          ) : null}
          {snap?.set_aside ? (
            <div className="notice state-idle">
              <strong>{int(snap.set_aside)} images set aside.</strong> A cancelled job left them
              unsegmented. They are not failures and are not counted in the backlog.
            </div>
          ) : null}

          <Verdict snap={snap} />

          <div className="row col-4">
            <Metric label="Segmentation rate" value={rate(snap?.current_rate)} unit="img/s"
                    state={v.cls}
                    sub={`30s average · ${rate(snap?.avg_rate)} img/s overall`} />
            <Metric label="Backlog trend" value={nf(snap?.backlog_trend_per_min, 1)} unit="img/min"
                    state={v.cls}
                    sub={snap?.backlog_trend_per_min > 0 ? 'growing' : 'flat or clearing'} />
            <Metric label="Time to clear backlog" value={eta ?? '—'}
                    sub={eta ? snap.eta_note : (snap?.eta_note ?? 'not enough data')} />
            <Metric label="Segmented" value={int(snap?.segmented)}
                    sub={`of ${int(snap?.found)} found · ${int(snap?.failed)} failed`} />
          </div>

          <div id="queues">
            <Queue queue={queue} onChanged={() => { refresh(); setQueueNonce((n) => n + 1) }} />
          </div>

          <div id="charts" className="stack">
            <Card title="Backlog"
                  aside={<span className="chart-note">images waiting, last 30 min</span>}
                  note="This is the chart that answers the question: a flat or falling line means the machine is coping.">
              <Chart hero data={series} yLabel="images"
                     lines={[{ key: 'backlog', name: 'Backlog', color: stateColor(v.key) }]} />
            </Card>

            <div className="row col-2">
              <Card title="Throughput" aside={<span className="chart-note">images/s</span>}>
                <Chart data={series}
                       lines={[{ key: 'rate', name: 'img/s', color: token('--primary', '#1ABB9C') }]} />
              </Card>
              <Card title="Found vs segmented" aside={<span className="chart-note">cumulative</span>}>
                <Chart data={series} lines={[
                  { key: 'found', name: 'Found', color: token('--text-muted', '#8a93a3') },
                  { key: 'segmented', name: 'Segmented', color: token('--primary', '#1ABB9C') },
                ]} />
              </Card>
            </div>
          </div>

          <div id="resources" className="row col-2">
            <Card title="Compute"
                  aside={<span className="chart-note">{snap?.gpu ? 'GPU / CPU / RAM' : 'CPU / RAM — no NVIDIA GPU'}</span>}>
              <Chart data={series} lines={[
                { key: 'cpu', name: 'CPU %', color: token('--azure', '#4299e1') },
                { key: 'ram', name: 'RAM %', color: token('--text-muted', '#8a93a3') },
                ...(snap?.gpu ? [{ key: 'gpu', name: 'GPU %', color: token('--primary', '#1ABB9C') }] : []),
              ]} />
            </Card>
            <Card title="Host">
              <div className="row col-2">
                <Metric label="CPU" value={nf(snap?.cpu_pct, 0)} unit="%" sub=" " />
                <Metric label="RAM" value={nf(snap?.ram_pct, 0)} unit="%"
                        sub={`${bytes(snap?.ram_used_bytes)} of ${bytes(snap?.ram_total_bytes)}`} />
                <Metric label="Disk free" value={bytes(snap?.disk_free_bytes)}
                        sub={`of ${bytes(snap?.disk_total_bytes)}`} />
                <Metric label="Input on disk" value={bytes(snap?.input_bytes)}
                        sub={`${int(snap?.worker?.input_files_on_disk)} TIFFs`} />
                {snap?.gpu ? (
                  <>
                    <Metric label="GPU" value={nf(snap.gpu.gpu_util_pct, 0)} unit="%"
                            sub={`${nf(snap.gpu.gpu_temp_c, 0)}°C · ${nf(snap.gpu.gpu_power_w, 0)} W`} />
                    <Metric label="VRAM" value={nf(snap.gpu.vram_used_mb, 0)} unit="MB"
                            sub={`of ${nf(snap.gpu.vram_total_mb, 0)} MB`} />
                  </>
                ) : null}
                <Metric label="Batch size" value={int(snap?.worker?.batch_size)}
                        sub={`${int(snap?.worker?.oom_events)} OOM retries`} />
                <Metric label="Uptime" value={dur(snap?.elapsed_s)} sub=" " />
              </div>
            </Card>
          </div>

          <Card id="machine" title="Machine spec"
                aside={(
                  <button className="btn btn-ghost btn-sm"
                          onClick={() => navigator.clipboard?.writeText(specs?.text ?? '')}>
                    Copy
                  </button>
                )}>
            <pre className="spec-dump">{specs?.text || 'Capturing…'}</pre>
          </Card>

          <Card id="failures" title="Failures"
                aside={<span className="chart-note">{int(failures?.failures?.length ?? 0)} total</span>}>
            {failures?.failures?.length ? (
              <table className="table table-failures">
                <thead><tr><th>File</th><th>When</th><th>Error</th></tr></thead>
                <tbody>
                  {failures.failures.slice(0, 50).map((f) => (
                    <tr key={f.path}>
                      <td className="cell-mono">{f.path.split('/').slice(-2).join('/')}</td>
                      <td className="num">{f.done_at ? clock(f.done_at) : '—'}</td>
                      <td className="error">{f.error}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted" style={{ margin: 0 }}>
                No failures. Unreadable files are retried five times before they land here.
              </p>
            )}
          </Card>

          <Card title="Access">
            <TokenBox required={config?.auth_required} />
            {!config?.auth_required ? (
              <p className="muted" style={{ margin: 0 }}>
                No token is set, so anyone who can reach this page can pause, cancel and queue
                jobs. Set <code>MM_AUTH_TOKEN</code> before exposing it beyond a trusted network.
              </p>
            ) : null}
          </Card>

          <footer className="footer">
            <span className="muted">
              {config ? `${config.model} on ${config.device} · input ${config.input_dir}` : ''}
            </span>
          </footer>
        </div>
      </main>
    </>
  )
}
