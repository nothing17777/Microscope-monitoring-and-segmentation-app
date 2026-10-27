import React, { useState } from 'react'
import { post } from './api'
import { dur, int, nf, rate } from './format'

// Phones are the realistic client here: the labels get switched standing at
// the microscope, not at the desk. Everything below is one tap.

const BATCH_PRESETS = [1, 2, 4, 8, 16, 32]

// Offered until the study has used anything; after that the buttons are the
// phases actually seen, so the list is the experiment's own vocabulary.
const SEED_PHASES = ['acquisition_only', 'solo_sweep_bs4', 'concurrent_bs8']

const slug = (s) => s.trim().split(/\s+/).join('_').replace(/^_+|_+$/g, '').slice(0, 64)

export function PhaseBar({ phase, batchSize, known, onChanged }) {
  const [custom, setCustom] = useState('')
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const act = async (what, fn) => {
    setBusy(what); setError(null)
    try { await fn(); onChanged() } catch (e) { setError(e.message) } finally { setBusy(null) }
  }
  const setPhase = (name) => act(`p:${name}`, () => post('/api/phase', { name }))
  const setBatch = (n) => act(`b:${n}`, () => post('/api/config/batch-size', { n }))

  const options = Array.from(new Set([...(known ?? []), ...SEED_PHASES]))
    .filter((p) => p && p !== 'unlabeled')

  return (
    <div className="card">
      <div className="card-header">
        <h3 className="card-title">Experiment</h3>
        <span className="chart-note">
          every sample and every batch is tagged with these
        </span>
      </div>
      <div className="card-body stack-sm">
        <div className="exp-row">
          <span className="exp-label">Phase</span>
          <span className={`exp-current ${phase === 'unlabeled' ? 'is-unset' : ''}`}>
            {phase ?? '—'}
          </span>
        </div>
        <div className="chip-row">
          {options.map((p) => (
            <button key={p} className={`chip ${p === phase ? 'is-on' : ''}`}
                    disabled={busy !== null} onClick={() => setPhase(p)}>
              {busy === `p:${p}` ? '…' : p}
            </button>
          ))}
          <button className={`chip ${phase === 'unlabeled' ? 'is-on' : ''}`}
                  disabled={busy !== null} onClick={() => setPhase('unlabeled')}>
            unlabeled
          </button>
        </div>
        <form className="inline-form" onSubmit={(e) => {
          e.preventDefault()
          const name = slug(custom)
          if (name) setPhase(name).then(() => setCustom(''))
        }}>
          <div className="field" style={{ flex: '1 1 12rem' }}>
            <label htmlFor="phase-new">New phase label</label>
            <input id="phase-new" value={custom} maxLength={64} autoComplete="off"
                   placeholder="concurrent_bs16"
                   onChange={(e) => setCustom(e.target.value)} />
          </div>
          <button className="btn btn-primary" disabled={busy !== null || !slug(custom)}>
            Set phase
          </button>
        </form>

        <div className="exp-row">
          <span className="exp-label">Batch size</span>
          <span className="exp-current">{int(batchSize)}</span>
          <span className="chart-note">takes effect on the next batch — no restart</span>
        </div>
        <div className="chip-row">
          {BATCH_PRESETS.map((n) => (
            <button key={n} className={`chip ${n === batchSize ? 'is-on' : ''}`}
                    disabled={busy !== null} onClick={() => setBatch(n)}>
              {busy === `b:${n}` ? '…' : n}
            </button>
          ))}
        </div>
        {error ? <div className="form-error">{error}</div> : null}
      </div>
    </div>
  )
}

const pctTxt = (f) => (f === null || f === undefined ? '—' : `${Math.round(f * 100)}%`)
const mm = (s, d = 0) => (s ? `${nf(s.mean, d)} / ${nf(s.max, d)}` : '—')

export function PhaseTable({ report }) {
  if (!report) return <div className="notice state-idle">Loading the report…</div>
  const { phases, tuning, contention } = report
  return (
    <>
      <div className="card">
        <div className="card-header">
          <h3 className="card-title">Contention cost</h3>
          <a className="chart-note" href="/api/report.md">report.md</a>
        </div>
        <div className="card-body">
          {contention.available ? (
            <>
              <div className="figure-big">
                <div className="figure-label">Concurrent throughput, as a share of solo</div>
                <div className="figure-value">{nf(contention.percent_of_solo, 1)}
                  <span className="unit">%</span></div>
                <div className="figure-sub">{contention.basis}</div>
              </div>
              <p className="muted" style={{ marginBottom: 0 }}>
                Solo {rate(contention.solo_images_per_s)} img/s
                {' → '}concurrent {rate(contention.concurrent_images_per_s)} img/s.
                Contention cost {nf(contention.contention_cost_pct, 1)}%.
              </p>
              {contention.note ? (
                <p className="form-error" style={{ marginBottom: 0 }}>{contention.note}.</p>
              ) : null}
            </>
          ) : (
            <p className="muted" style={{ margin: 0 }}>
              Not enough data: {contention.note}. The headline needs one phase named
              {' '}<code>*solo*</code> and one named <code>*concurrent*</code>, each with at
              least {report.min_samples_per_phase} samples.
            </p>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header"><h3 className="card-title">Tuning — by batch size</h3>
          <span className="chart-note">throughput against acquisition impact</span></div>
        <div className="card-body" style={{ overflowX: 'auto' }}>
          {tuning.length ? (
            <table className="data">
              <thead><tr>
                <th>Batch</th><th>img/s</th><th>Inter-arrival mean</th>
                <th>Inter-arrival p95</th><th>GPU idle</th><th>Time</th><th>Phases</th>
              </tr></thead>
              <tbody>
                {tuning.map((t) => (
                  <tr key={t.batch_size}>
                    <td className="num">{t.batch_size}</td>
                    <td className="num">{rate(t.images_per_s)}</td>
                    <td className="num">{nf(t.inter_arrival_mean_s, 2)} s</td>
                    <td className="num">{nf(t.inter_arrival_p95_s, 2)} s</td>
                    <td className="num">{pctTxt(t.gpu_idle_fraction)}</td>
                    <td className="num">{dur(t.duration_s)}</td>
                    <td>{t.phases.join(', ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted" style={{ margin: 0 }}>
              No phase has held a single batch size for {report.min_samples_per_phase} samples
              yet. Run the sweep, or leave a batch size alone for longer.
            </p>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header"><h3 className="card-title">Phase comparison</h3>
          <span className="chart-note">{report.samples} samples</span></div>
        <div className="card-body" style={{ overflowX: 'auto' }}>
          <table className="data">
            <thead><tr>
              <th>Phase</th><th>Batch</th><th>Duration</th><th>img/s mean</th><th>p50</th>
              <th>p95</th><th>Inter-arr mean</th><th>Inter-arr p95</th>
              <th>CPU mean/max</th><th>RAM mean/max</th><th>GPU mean/max</th>
              <th>VRAM mean/max</th><th>Disk R mean/max</th><th>Disk W mean/max</th>
              <th>GPU idle</th>
            </tr></thead>
            <tbody>
              {phases.map((p) => (
                <tr key={p.phase}>
                  <td>{p.phase}</td>
                  <td className="num">{p.batch_size ?? '—'}</td>
                  <td className="num">{dur(p.duration_s)}</td>
                  {p.enough_data ? (
                    <>
                      <td className="num">{rate(p.throughput.mean)}</td>
                      <td className="num">{rate(p.throughput.p50)}</td>
                      <td className="num">{rate(p.throughput.p95)}</td>
                      <td className="num">{nf(p.inter_arrival.mean, 2)} s</td>
                      <td className="num">{nf(p.inter_arrival_p95.mean, 2)} s</td>
                      <td className="num">{mm(p.cpu_pct)}%</td>
                      <td className="num">{mm(p.ram_pct)}%</td>
                      <td className="num">{mm(p.gpu_util_pct)}%</td>
                      <td className="num">{mm(p.vram_used_mb)} MB</td>
                      <td className="num">{mm(p.disk_read_mbs, 1)}</td>
                      <td className="num">{mm(p.disk_write_mbs, 1)}</td>
                      <td className="num">{pctTxt(p.gpu_idle_fraction)}</td>
                    </>
                  ) : (
                    // The ETA rule, applied here: a thin phase gets no numbers
                    // at all rather than numbers with a caveat beside them.
                    <td className="muted" colSpan={12}>{p.note}</td>
                  )}
                </tr>
              ))}
              {phases.length ? null : (
                <tr><td className="muted" colSpan={15}>Nothing sampled yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
