import React, { useEffect, useMemo, useRef, useState } from 'react'
import Chart, { stateColor, toSeries, token } from './Charts'
import Plate from './Plate'
import Queue from './Queue'
import RunDetail from './RunDetail'
import { getToken, post, setToken } from './api'
import { useLive, usePoll } from './useLive'
import { bytes, clock, dur, durRange, int, nf, rate } from './format'

// --- routing (hash only; this is one page behind a token, not an app) -------

function useRoute() {
  const [hash, setHash] = useState(() => window.location.hash || '#/')
  useEffect(() => {
    const on = () => setHash(window.location.hash || '#/')
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  const m = /^#\/runs\/(\d+)/.exec(hash)
  if (m) return { page: 'run', runId: Number(m[1]) }
  if (hash.startsWith('#/runs')) return { page: 'runs' }
  return { page: 'dashboard' }
}

function useTheme() {
  const [theme, setTheme] = useState(
    () => document.documentElement.getAttribute('data-theme') || 'dark')
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try { localStorage.setItem('st_theme', theme) } catch { /* private mode */ }
  }, [theme])
  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))]
}

// --- state ------------------------------------------------------------------

const SEVERITY = { idle: 0, 'keeping-up': 0, slipping: 1, behind: 2 }

function segState(snap) {
  if (!snap) return { key: 'idle', cls: 'state-idle', headline: 'Connecting…', sub: '' }
  if (!snap.found) {
    return { key: 'idle', cls: 'state-idle', headline: 'Waiting for images',
      sub: 'Nothing has reached the input folder yet.' }
  }
  if (snap.falling_behind) {
    return { key: 'behind', cls: 'state-bad', headline: 'Falling behind',
      sub: `Backlog growing by about ${nf(snap.backlog_trend_per_min, 0)} images a minute.` }
  }
  if (snap.backlog > 0 && snap.backlog_trend_per_min > 0) {
    return { key: 'slipping', cls: 'state-warn', headline: 'Slipping',
      sub: 'Backlog rising slowly — not yet falling behind.' }
  }
  return { key: 'keeping-up', cls: 'state-ok', headline: 'Keeping up',
    sub: 'Images are being cleared at least as fast as they arrive.' }
}

function acqState(run, adapter) {
  if (!run) {
    return { key: 'idle', cls: 'state-idle', headline: 'No run in progress',
      sub: adapter?.can_control
        ? 'Start a run below.'
        : 'Start the run in LumaViewPro, then add it below to follow it here.' }
  }
  if (run.status === 'paused') {
    return { key: 'paused', cls: 'state-warn', headline: 'Paused', sub: run.name }
  }
  const done = run.expected_image_count
    ? Math.round((run.captured / run.expected_image_count) * 100) : 0
  return { key: 'running', cls: 'state-busy', headline: `${done}% captured`, sub: run.name }
}

// An ETA block is either a usable range or nothing. Written as a function
// because `eta?.eta_confidence !== 'none'` is TRUE when eta is missing, which
// is exactly the case that must not reach durRange().
function etaText(eta) {
  if (!eta || eta.eta_confidence === 'none') return { value: '—', sub: eta?.eta_note ?? '' }
  return {
    value: durRange(eta.eta_low, eta.eta_high),
    sub: eta.eta_confidence === 'low' ? 'rough' : (eta.eta_note ?? ''),
  }
}

function Figure({ label, value, unit, sub, big }) {
  const range = typeof value === 'string' && value.includes('–')
  return (
    <div className={big ? 'figure-big' : ''}>
      <div className="figure-label">{label}</div>
      <div className={`figure-value ${range ? 'is-range' : ''}`}>
        {value}{unit ? <span className="unit">{unit}</span> : null}
      </div>
      <div className="figure-sub">{sub}</div>
    </div>
  )
}

function Panel({ state, children, kicker }) {
  const prev = useRef(state.key)
  const [flash, setFlash] = useState(false)
  useEffect(() => {
    const worse = (SEVERITY[state.key] ?? 0) > (SEVERITY[prev.current] ?? 0)
    prev.current = state.key
    if (!worse) return undefined
    setFlash(true)
    const t = setTimeout(() => setFlash(false), 1300)
    return () => clearTimeout(t)
  }, [state.key])
  return (
    <section className={`panel ${state.cls} ${flash ? 'just-changed' : ''}`}>
      <div>
        <div className="panel-kicker">{kicker}</div>
        <div className="panel-headline">{state.headline}</div>
        <p className="panel-sub">{state.sub}</p>
      </div>
      {children}
    </section>
  )
}

// --- new run ----------------------------------------------------------------

function NewRun({ onCreated, canControl }) {
  const [f, setF] = useState({ name: '', plate: '', expected_image_count: '', notes: '' })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError(null)
    try {
      await post('/api/runs', { ...f, expected_image_count: Number(f.expected_image_count) })
      setF({ name: '', plate: '', expected_image_count: '', notes: '' })
      onCreated()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h3 className="card-title">{canControl ? 'Start a run' : 'Track a run'}</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {canControl ? 'control enabled (stubbed)' : 'the microscope is started in LumaViewPro'}
        </span>
      </div>
      <form className="inline-form" onSubmit={submit} style={{ padding: 'var(--space-4)' }}>
        <div className="field" style={{ flex: '2 1 10rem' }}>
          <label htmlFor="name">Run name</label>
          <input id="name" required maxLength={120} value={f.name}
                 placeholder="Tue mock test" onChange={set('name')} />
        </div>
        <div className="field" style={{ flex: '1 1 8rem' }}>
          <label htmlFor="plate">Plate</label>
          <input id="plate" maxLength={120} value={f.plate}
                 placeholder="MSC-96-A" onChange={set('plate')} />
        </div>
        <div className="field" style={{ flex: '1 1 8rem' }}>
          <label htmlFor="exp">Expected images</label>
          <input id="exp" required type="number" min="1" value={f.expected_image_count}
                 placeholder="15360" onChange={set('expected_image_count')} />
        </div>
        <div className="field" style={{ flex: '2 1 10rem' }}>
          <label htmlFor="notes">Notes</label>
          <input id="notes" maxLength={4000} value={f.notes}
                 placeholder="2x2, 20 slices, phase + green" onChange={set('notes')} />
        </div>
        <button className="btn btn-primary" disabled={busy}>{busy ? 'Adding…' : 'Add run'}</button>
        {error ? <div className="form-error" style={{ flex: '1 1 100%' }}>{error}</div> : null}
      </form>
    </div>
  )
}

// --- pages ------------------------------------------------------------------

function RunList({ runs }) {
  if (!runs) return <div className="notice state-idle">Loading runs…</div>
  if (!runs.length) {
    return (
      <div className="card"><div className="queue-empty">
        <strong>No runs yet</strong>
        The first run you add will appear here, and stay for comparison afterwards.
      </div></div>
    )
  }
  return (
    <div className="card">
      <div className="card-header"><h3 className="card-title">All runs</h3>
        <span className="chart-note">{runs.length}</span></div>
      <div className="card-body" style={{ overflowX: 'auto' }}>
        <table className="data">
          <thead>
            <tr>
              <th>Run</th><th>Plate</th><th>Status</th><th>Captured</th>
              <th>Segmented</th><th>Failed</th><th>Wells</th><th>Elapsed</th><th>Started</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td><a href={`#/runs/${r.id}`}>{r.name}</a></td>
                <td>{r.plate || '—'}</td>
                <td>{r.status}</td>
                <td className="num">{int(r.captured)} / {int(r.expected_image_count)}</td>
                <td className="num">{int(r.segmented)}</td>
                <td className="num">{int(r.failed)}</td>
                <td className="num">{int(r.wells_done)}/{int(r.wells_total)}</td>
                <td className="num">{dur(r.elapsed_s)}</td>
                <td className="num">{r.started_at ? clock(r.started_at) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Alerts({ events, configured, onTest, busy }) {
  return (
    <div className="card">
      <div className="card-header"><h3 className="card-title">Alerts</h3>
        <button className="btn btn-ghost btn-sm" disabled={busy || !configured} onClick={onTest}>
          Send test
        </button>
      </div>
      <div className="card-body">
        {!configured ? (
          <p className="muted" style={{ marginTop: 0 }}>
            No webhook is configured, so an unattended run will alert nobody. Set
            {' '}<code>MM_ST_WEBHOOK_URL</code> to a Slack incoming webhook.
          </p>
        ) : null}
        {events?.length ? (
          <table className="data">
            <thead><tr><th>When</th><th>Kind</th><th>Delivered</th><th>Message</th></tr></thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td className="num">{clock(e.ts)}</td>
                  <td>{e.kind}</td>
                  <td>{e.delivered ? 'yes' : 'no'}</td>
                  <td>{e.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted" style={{ margin: 0 }}>Nothing has fired.</p>}
      </div>
    </div>
  )
}

function TokenBox({ required }) {
  const [value, setValue] = useState(getToken())
  const [saved, setSaved] = useState(false)
  if (!required) {
    return (
      <p className="muted" style={{ margin: 0 }}>
        No token is set, so anyone who can reach this page can start, pause and cancel runs.
        Set <code>MM_ST_AUTH_TOKEN</code> before exposing it beyond a trusted network.
      </p>
    )
  }
  return (
    <form className="inline-form" onSubmit={(e) => {
      e.preventDefault(); setToken(value); setSaved(true); setTimeout(() => setSaved(false), 2000)
    }}>
      <div className="field" style={{ flex: '1 1 12rem' }}>
        <label htmlFor="tok">Access token</label>
        <input id="tok" type="password" value={value} autoComplete="off"
               placeholder="needed to start, pause or cancel"
               onChange={(e) => setValue(e.target.value)} />
      </div>
      <button className="btn btn-primary">{saved ? 'Saved' : 'Save'}</button>
    </form>
  )
}

// --- shell ------------------------------------------------------------------

const NAV = [
  ['#/', 'Dashboard'],
  ['#/runs', 'Run history'],
]

export default function App() {
  const route = useRoute()
  const { snap, connected, stale, refresh } = useLive()
  const [theme, toggleTheme] = useTheme()
  const [navOpen, setNavOpen] = useState(false)
  const [nonce, setNonce] = useState(0)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)
  const config = usePoll('/api/config', 60000)
  const history = usePoll('/api/history?minutes=30', 10000)
  const runsList = usePoll(`/api/runs?limit=100&n=${nonce}`, 15000)
  const events = usePoll(`/api/events?n=${nonce}`, 20000)
  const queue = snap?.queue
  const activeRun = queue?.acquisition?.[0] ?? null
  const plate = usePoll(activeRun ? `/api/runs/${activeRun.id}/plate` : null, 5000)

  const [, force] = useState(0)
  useEffect(() => { force((n) => n + 1) }, [theme])

  const series = useMemo(() => toSeries(history?.samples), [history])
  const seg = segState(snap)
  const acq = acqState(activeRun, queue?.adapter)
  const changed = () => { refresh(); setNonce((n) => n + 1) }

  const sendTest = async () => {
    setBusy(true)
    try {
      const r = await post('/api/notify/test')
      setToast(r.sent ? 'Test alert delivered.' : `Not sent: ${r.reason}`)
    } catch (e) { setToast(e.message) } finally { setBusy(false); setNonce((n) => n + 1) }
  }

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to content</a>

      <aside className={`sidebar ${navOpen ? 'open' : ''}`}>
        <div className="sidebar-brand"><span className="brand-name">Microscope Station</span></div>
        <nav className="sidebar-nav">
          <div className="nav-group">
            <div className="nav-label">Control</div>
            {NAV.map(([href, label]) => (
              <a key={href} className="nav-link" href={href} onClick={() => setNavOpen(false)}>
                {label}
              </a>
            ))}
          </div>
        </nav>
        <div className="sidebar-footer">
          <div className="sidebar-user">
            <div className="sidebar-user-info">
              <div className="name">{config?.device ?? '—'}</div>
              <div className="role">{config?.dry_run ? 'dry-run' : (config?.model ?? '—')}</div>
            </div>
          </div>
        </div>
      </aside>
      <div className="sidebar-backdrop" hidden={!navOpen} onClick={() => setNavOpen(false)} />

      <header className="topbar">
        <div className="topbar-left">
          <button className="sidebar-toggle" onClick={() => setNavOpen((o) => !o)}
                  aria-label="Toggle navigation">☰</button>
          <nav className="breadcrumb"><span className="current">
            {route.page === 'dashboard' ? `${acq.headline} · ${seg.headline}` : 'Run history'}
          </span></nav>
        </div>
        <div className="topbar-right">
          <span className="muted" style={{ fontSize: 12 }}>{connected ? 'live' : 'reconnecting'}</span>
          <button className="btn btn-ghost btn-sm theme-toggle" onClick={toggleTheme}
                  aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>
            <svg className="theme-icon-light" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="4.2" />
              <path d="M12 2.6v2.1M12 19.3v2.1M4.2 4.2l1.5 1.5M18.3 18.3l1.5 1.5M2.6 12h2.1M19.3 12h2.1M4.2 19.8l1.5-1.5M18.3 5.7l1.5-1.5" />
            </svg>
            <svg className="theme-icon-dark" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M20.5 14.4A8.5 8.5 0 1 1 9.6 3.5a6.8 6.8 0 0 0 10.9 10.9Z" />
            </svg>
            <span>{theme === 'dark' ? 'Light' : 'Dark'}</span>
          </button>
        </div>
      </header>

      <main className="main" id="main-content">
        <div className="page-wrapper stack">
          {route.page === 'run' ? (
            <RunDetail runId={route.runId} onBack={() => { window.location.hash = '#/runs' }} />
          ) : route.page === 'runs' ? (
            <>
              <div className="page-header"><h1 className="page-title">Run history</h1></div>
              <RunList runs={runsList?.runs} />
            </>
          ) : (
            <>
              <div className="page-header page-header-row">
                <div>
                  <div className="page-pretitle">
                    {config?.dry_run ? 'DRY RUN — masks are a threshold, not Cellpose' : 'Live'}
                    {' · '}{queue?.adapter?.mode === 'controlled'
                      ? 'acquisition control enabled (stubbed)' : 'watching files'}
                  </div>
                  <h1 className="page-title">Station</h1>
                </div>
              </div>

              {toast ? <div className="notice state-idle"><strong>Alerts.</strong> {toast}</div> : null}
              {stale ? (
                <div className="notice state-warn">
                  <strong>Not updating.</strong> No data for more than 15 seconds — the numbers
                  below are the last ones received.
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
                  <strong>Segmentation is paused.</strong> Images are still being discovered and
                  the backlog is growing.
                </div>
              ) : null}
              {snap?.set_aside ? (
                <div className="notice state-idle">
                  <strong>{int(snap.set_aside)} images set aside.</strong> Left unsegmented by a
                  cancelled run. Not failures, and not counted in the backlog.
                </div>
              ) : null}

              {/* Two panels, equal weight: the microscope and the GPU. */}
              <div className="row col-2">
                <Panel state={acq} kicker="Acquisition">
                  <div className="figures">
                    <Figure big label="Images captured" value={int(activeRun?.captured ?? 0)}
                            sub={activeRun ? `of ${int(activeRun.expected_image_count)}` : '—'} />
                    <Figure label="Wells"
                            value={activeRun ? `${int(activeRun.wells_done)}/${int(activeRun.wells_total)}` : '—'}
                            sub={activeRun ? `${int(activeRun.wells_seen)} touched` : ''} />
                    <Figure label="Capture rate"
                            value={rate(activeRun?.capture_rate ? activeRun.capture_rate * 60 : null)}
                            unit="img/min" sub={activeRun?.acquisition_eta?.eta_note ?? ''} />
                    <Figure label="Elapsed" value={dur(activeRun?.elapsed_s)}
                            sub={activeRun?.started_at ? `since ${clock(activeRun.started_at)}` : ''} />
                    <Figure label="ETA" {...etaText(activeRun?.acquisition_eta)} />
                  </div>
                </Panel>

                <Panel state={seg} kicker="Segmentation">
                  <div className="figures">
                    <Figure big label="Images waiting" value={int(snap?.backlog ?? 0)}
                            sub={snap?.backlog === 1 ? 'image in the backlog' : 'in the backlog'} />
                    <Figure label="Rate" value={rate(snap?.current_rate)} unit="img/s"
                            sub={`30s avg · ${rate(snap?.avg_rate)} overall`} />
                    <Figure label="Trend" value={nf(snap?.backlog_trend_per_min, 1)} unit="img/min"
                            sub={snap?.backlog_trend_per_min > 0 ? 'growing' : 'flat or clearing'} />
                    <Figure label="Segmented" value={int(snap?.segmented)}
                            sub={`of ${int(snap?.found)} · ${int(snap?.failed)} failed`} />
                    <Figure label="Clear backlog in" {...etaText(snap)} />
                  </div>
                </Panel>
              </div>

              {activeRun ? (
                <div className="card">
                  <div className="card-header">
                    <h3 className="card-title">Plate — {activeRun.name}</h3>
                    <a className="chart-note" href={`#/runs/${activeRun.id}`}>run detail</a>
                  </div>
                  <div className="card-body"><Plate plate={plate} /></div>
                </div>
              ) : null}

              <Queue queue={queue} onChanged={changed} />
              <NewRun onCreated={changed} canControl={queue?.adapter?.can_control} />

              <div className="card">
                <div className="card-header"><h3 className="card-title">Backlog</h3>
                  <span className="chart-note">last 30 min</span></div>
                <div className="card-body">
                  <Chart hero data={series} yLabel="images"
                         lines={[{ key: 'backlog', name: 'Backlog', color: stateColor(seg.key) }]} />
                </div>
              </div>

              <div className="row col-2">
                <div className="card">
                  <div className="card-header"><h3 className="card-title">Throughput</h3></div>
                  <div className="card-body">
                    <Chart data={series} lines={[{ key: 'rate', name: 'img/s',
                            color: token('--primary', '#1ABB9C') }]} />
                  </div>
                </div>
                <div className="card">
                  <div className="card-header"><h3 className="card-title">Host</h3>
                    <span className="chart-note">{snap?.gpu ? 'GPU / CPU / RAM' : 'CPU / RAM'}</span></div>
                  <div className="card-body">
                    <Chart data={series} lines={[
                      { key: 'cpu', name: 'CPU %', color: token('--azure', '#4299e1') },
                      { key: 'ram', name: 'RAM %', color: token('--text-muted', '#8a93a3') },
                      ...(snap?.gpu ? [{ key: 'gpu', name: 'GPU %',
                                         color: token('--primary', '#1ABB9C') }] : []),
                    ]} />
                  </div>
                </div>
              </div>

              <Alerts events={events?.events} configured={events?.webhook_configured}
                      onTest={sendTest} busy={busy} />

              <div className="card">
                <div className="card-header"><h3 className="card-title">Access</h3>
                  <span className="chart-note">
                    disk {bytes(snap?.disk_free_bytes)} free
                  </span></div>
                <div className="card-body"><TokenBox required={config?.auth_required} /></div>
              </div>
            </>
          )}

          <footer className="footer">
            <span className="muted">
              {config ? `${config.model} on ${config.device} · ${config.input_dir}` : ''}
            </span>
          </footer>
        </div>
      </main>
    </>
  )
}
