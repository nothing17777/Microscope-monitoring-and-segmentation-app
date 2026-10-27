import React, { useEffect, useState } from 'react'
import Plate from './Plate'
import Chart, { toSeries, token } from './Charts'
import { getJSON } from './api'
import { bytes, clock, dur, int, rate } from './format'

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

export default function RunDetail({ runId, onBack }) {
  const [detail, setDetail] = useState(null)
  const [history, setHistory] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    setDetail(null); setHistory(null); setError(null)
    getJSON(`/api/runs/${runId}`)
      .then((d) => { if (!alive) return; if (d.detail) setError(d.detail); else setDetail(d) })
      .catch((e) => alive && setError(String(e)))
    getJSON(`/api/runs/${runId}/history`)
      .then((d) => alive && setHistory(d.samples))
      .catch(() => {})
    return () => { alive = false }
  }, [runId])

  if (error) {
    return (
      <div className="notice state-bad">
        <strong>Run not found.</strong> {error}
        <button className="btn btn-ghost btn-sm" onClick={onBack}>Back to runs</button>
      </div>
    )
  }
  if (!detail) return <div className="notice state-idle">Loading run #{runId}…</div>

  const r = detail.run
  const series = toSeries(history)
  const live = r.status === 'running' || r.status === 'paused'

  return (
    <div className="stack">
      <div className="page-header page-header-row">
        <div>
          <div className="page-pretitle">
            Run #{r.id} · {r.plate || 'no plate'} · {r.status}
            {live ? ' · live' : ' · replay from stored telemetry'}
          </div>
          <h1 className="page-title">{r.name}</h1>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={onBack}>All runs</button>
      </div>

      {r.notes ? <div className="notice state-idle"><strong>Notes.</strong> {r.notes}</div> : null}

      <div className="card">
        <div className="card-body figures">
          <Figure label="Images captured" value={int(r.captured)}
                  sub={`of ${int(r.expected_image_count)} expected`} />
          <Figure label="Segmented" value={int(r.segmented)}
                  sub={`${int(r.backlog)} waiting · ${int(r.failed)} failed`} />
          <Figure label="Wells" value={`${int(r.wells_done)}/${int(r.wells_total)}`}
                  sub={`${int(r.wells_seen)} seen`} />
          <Figure label="Elapsed" value={dur(r.elapsed_s)}
                  sub={r.started_at ? `started ${clock(r.started_at)}` : 'not started'} />
          <Figure label="Capture rate" value={rate(r.capture_rate ? r.capture_rate * 60 : null)}
                  unit="img/min" sub={r.acquisition_eta?.eta_note ?? ''} />
          <Figure label="Set aside" value={int(r.set_aside)}
                  sub={r.set_aside ? 'left by a cancel' : 'none'} />
        </div>
      </div>

      <div className="card">
        <div className="card-header"><h3 className="card-title">Plate</h3>
          <span className="chart-note">{r.wells_seen} of {r.wells_total} wells touched</span></div>
        <div className="card-body"><Plate plate={detail.plate} /></div>
      </div>

      <div className="card">
        <div className="card-header"><h3 className="card-title">Backlog</h3>
          <span className="chart-note">{series.length} samples</span></div>
        <div className="card-body">
          {series.length ? (
            <Chart hero data={series} yLabel="images"
                   lines={[{ key: 'backlog', name: 'Backlog', color: token('--primary', '#1ABB9C') }]} />
          ) : <p className="muted" style={{ margin: 0 }}>No telemetry was recorded for this run.</p>}
        </div>
      </div>

      {series.length ? (
        <div className="row col-2">
          <div className="card">
            <div className="card-header"><h3 className="card-title">Throughput</h3></div>
            <div className="card-body">
              <Chart data={series} lines={[{ key: 'rate', name: 'img/s',
                      color: token('--primary', '#1ABB9C') }]} />
            </div>
          </div>
          <div className="card">
            <div className="card-header"><h3 className="card-title">Found vs segmented</h3></div>
            <div className="card-body">
              <Chart data={series} lines={[
                { key: 'found', name: 'Found', color: token('--text-muted', '#8a93a3') },
                { key: 'segmented', name: 'Segmented', color: token('--primary', '#1ABB9C') },
              ]} />
            </div>
          </div>
        </div>
      ) : null}

      <div className="card">
        <div className="card-header"><h3 className="card-title">Failures</h3>
          <span className="chart-note">{detail.failures.length}</span></div>
        <div className="card-body">
          {detail.failures.length ? (
            <table className="data">
              <thead><tr><th>File</th><th>Well</th><th>When</th><th>Error</th></tr></thead>
              <tbody>
                {detail.failures.slice(0, 100).map((f) => (
                  <tr key={f.path}>
                    <td className="num">{f.path.split('/').slice(-1)[0]}</td>
                    <td className="num">{f.well || '—'}</td>
                    <td className="num">{f.done_at ? clock(f.done_at) : '—'}</td>
                    <td className="err">{f.error}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="muted" style={{ margin: 0 }}>None.</p>}
        </div>
      </div>
    </div>
  )
}
