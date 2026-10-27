import React, { useEffect, useMemo, useRef, useState } from 'react'
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

const HISTORY_MINUTES = 60
const C = {
  found: '#4cc2ff', segmented: '#3ddc84', backlog: '#ffb020', failed: '#ff4d4d',
  gpu: '#b388ff', vram: '#4cc2ff', cpu: '#3ddc84', ram: '#ffb020',
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
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : m ? `${m}m ${String(sec).padStart(2, '0')}s` : `${sec}s`
}

const clock = (ts) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

// --- data hooks -------------------------------------------------------------

function useSnapshot() {
  const [snap, setSnap] = useState(null)
  const [live, setLive] = useState(false)
  const ws = useRef(null)

  useEffect(() => {
    let closed = false
    let retry
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const sock = new WebSocket(`${proto}://${location.host}/ws`)
      ws.current = sock
      sock.onopen = () => setLive(true)
      sock.onmessage = (e) => setSnap(JSON.parse(e.data))
      sock.onclose = () => {
        setLive(false)
        if (!closed) retry = setTimeout(connect, 2000)
      }
      sock.onerror = () => sock.close()
    }
    connect()
    return () => { closed = true; clearTimeout(retry); ws.current?.close() }
  }, [])

  // Fallback so the tiles are populated before the first WS frame arrives.
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

// --- pieces -----------------------------------------------------------------

function Tile({ k, v, sub, color }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className="v" style={color ? { color } : undefined}>{v}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  )
}

function Chart({ title, data, series, yProps = {}, children }) {
  return (
    <div className="card">
      <div className="row">
        <h2>{title}</h2>
        {children}
      </div>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data} margin={{ top: 12, right: 18, left: 4, bottom: 4 }}>
          <CartesianGrid stroke="#27303d" vertical={false} />
          <XAxis dataKey="t" stroke="#8b99ad" tick={{ fontSize: 13 }} minTickGap={48} />
          <YAxis stroke="#8b99ad" tick={{ fontSize: 13 }} width={62} {...yProps} />
          <Tooltip
            contentStyle={{ background: '#141a23', border: '1px solid #27303d', fontSize: 14 }}
            labelStyle={{ color: '#8b99ad' }}
          />
          <Legend wrapperStyle={{ fontSize: 14 }} />
          {series.map((s) => (
            <Line
              key={s.key} type="monotone" dataKey={s.key} name={s.name}
              stroke={s.color} strokeWidth={2.5} dot={false} isAnimationActive={false}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function Specs() {
  const specs = useJson('/api/specs', 0)
  const [copied, setCopied] = useState(false)
  const text = specs?.text || ''
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove()
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }
  return (
    <div className="card">
      <div className="row">
        <h2>Machine spec</h2>
        <button onClick={copy} style={{ marginLeft: 'auto' }}>
          {copied ? 'Copied' : 'Copy'}
        </button>
        <a href="/api/specs.txt" target="_blank" rel="noreferrer">
          <button>Open specs.txt</button>
        </a>
      </div>
      <pre className="specs">{text || 'capturing…'}</pre>
    </div>
  )
}

function Failures() {
  const data = useJson('/api/failures', 10000)
  const rows = data?.failures || []
  return (
    <div className="card">
      <h2>Failed files ({rows.length})</h2>
      {rows.length === 0 ? (
        <div className="empty">No failures.</div>
      ) : (
        <div style={{ maxHeight: 440, overflow: 'auto' }}>
          <table className="fail">
            <thead>
              <tr><th>When</th><th>File</th><th>Error</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.path}>
                  <td style={{ whiteSpace: 'nowrap' }}>{r.done_at ? clock(r.done_at) : '—'}</td>
                  <td style={{ wordBreak: 'break-all' }}>{r.path}</td>
                  <td className="err">{r.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// --- page -------------------------------------------------------------------

export default function App() {
  const { snap, live } = useSnapshot()
  const cfg = useJson('/api/config', 0)
  const [histMin, setHistMin] = useState(HISTORY_MINUTES)
  const hist = useJson(`/api/history?minutes=${histMin}`, 5000)

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
  const vramTotalGb = gpu?.vram_total_mb ? gpu.vram_total_mb / 1024 : null

  return (
    <div className="wrap">
      <header className="top">
        <h1>Microscope Monitor</h1>
        <div className="chips">
          <span className={`chip ${live ? 'live' : 'dead'}`}>
            {live ? <b>LIVE</b> : <b>RECONNECTING</b>}
          </span>
          <span className="chip">device <b>{cfg?.device?.toUpperCase() || '—'}</b></span>
          <span className="chip">model <b>{cfg?.model || '—'}</b></span>
          <span className="chip">batch <b>{snap?.worker?.batch_size ?? cfg?.batch_size ?? '—'}</b></span>
          {snap?.worker?.oom_events ? (
            <span className="chip dead">OOM retries <b>{snap.worker.oom_events}</b></span>
          ) : null}
          <span className="chip">input <b>{cfg?.input_dir || '—'}</b></span>
        </div>
      </header>

      <div className={`backlog-banner ${behind ? 'bad' : 'ok'}`}>
        <div>
          <div className="label">Backlog</div>
          <div className="n">{snap ? snap.backlog : '—'}</div>
        </div>
        <div>
          <div className="verdict">
            {behind ? 'SEGMENTATION FALLING BEHIND' : 'KEEPING UP'}
          </div>
          <div className="trend">
            backlog trend over last 5 min:{' '}
            {snap ? `${snap.backlog_trend_per_min > 0 ? '+' : ''}${nf(snap.backlog_trend_per_min, 1)} images/min` : '—'}
            {' · '}segmenting {nf(snap?.current_rate, 2)} img/s vs {nf(snap?.avg_rate, 2)} img/s average
          </div>
        </div>
      </div>

      <div className="tiles">
        <Tile
          k="Segmented / found"
          v={<>{snap ? snap.segmented : '—'} <small>/ {snap ? snap.found : '—'}</small></>}
          sub={snap && snap.found ? `${nf((100 * snap.segmented) / snap.found, 1)}% complete` : null}
          color={C.segmented}
        />
        <Tile k="Backlog" v={snap ? snap.backlog : '—'} sub="pending, size-stable files"
              color={behind ? C.failed : C.backlog} />
        <Tile k="Current rate" v={<>{nf(snap?.current_rate, 2)} <small>img/s</small></>}
              sub={`${nf(snap?.avg_rate, 2)} img/s average`} />
        <Tile k="Elapsed" v={dur(snap?.acquisition_elapsed_s)}
              sub={`app up ${dur(snap?.elapsed_s)}`} />
        <Tile k="ETA to drain" v={dur(snap?.eta_s)} sub="at the current rate" />
        <Tile k="Disk free" v={bytes(snap?.disk_free_bytes)}
              sub={`${bytes(snap?.input_bytes)} written to input dir`} />
        <Tile k="Failed" v={snap ? snap.failed : '—'}
              color={snap?.failed ? C.failed : undefined} sub="see list below" />
        <Tile
          k="GPU"
          v={gpu ? <>{nf(gpu.gpu_util_pct)}<small>%</small></> : 'n/a'}
          sub={gpu
            ? `${nf(gpu.vram_used_mb / 1024, 1)} / ${nf(vramTotalGb, 1)} GB · ${nf(gpu.gpu_temp_c)}°C · ${nf(gpu.gpu_power_w)} W`
            : 'no nvidia-smi on this machine'}
          color={C.gpu}
        />
      </div>

      <div className="grid2">
        <Chart
          title="Images found vs segmented"
          data={series}
          series={[
            { key: 'found', name: 'found', color: C.found },
            { key: 'segmented', name: 'segmented', color: C.segmented },
          ]}
        >
          <select
            value={histMin}
            onChange={(e) => setHistMin(Number(e.target.value))}
            style={{
              marginLeft: 'auto', background: '#1b2330', color: '#e8eef7',
              border: '1px solid #27303d', borderRadius: 8, padding: '7px 10px', fontSize: 15,
            }}
          >
            {[5, 15, 30, 60, 180].map((m) => (
              <option key={m} value={m}>last {m} min</option>
            ))}
          </select>
        </Chart>
        <Chart
          title="Backlog"
          data={series}
          series={[
            { key: 'backlog', name: 'backlog', color: C.backlog },
            { key: 'failed', name: 'failed', color: C.failed },
          ]}
        />
        <Chart
          title="GPU utilisation (%) and VRAM (GB)"
          data={series}
          series={[
            { key: 'gpu', name: 'GPU util %', color: C.gpu },
            { key: 'vram', name: 'VRAM GB', color: C.vram },
          ]}
        />
        <Chart
          title="CPU (%) and RAM (%)"
          data={series}
          yProps={{ domain: [0, 100] }}
          series={[
            { key: 'cpu', name: 'CPU %', color: C.cpu },
            { key: 'ram', name: 'RAM %', color: C.ram },
          ]}
        />
      </div>

      <div className="grid2">
        <Specs />
        <Failures />
      </div>
    </div>
  )
}
