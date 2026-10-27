import React from 'react'
import { bytes, nf } from './format'

// What this workstation looks like when everything is fine, measured on it
// (RTX 5090, Core Ultra 9 285K, 62 GB) during a full-plate run on 2026-10-27.
// Kept in step with docs/WORKSTATION.md. These are this machine's numbers, not
// general limits: on other hardware, re-measure before trusting them.

// Live values are averaged over the last ~30 s of samples: GPU load swings
// between 0 and 100% inside every batch, so one sample says little.
function recent(series, key, n = 15) {
  const xs = (series ?? []).slice(-n).map((d) => d[key]).filter((v) => v != null)
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null
}

const ROWS = [
  {
    what: 'GPU load',
    normal: '80–100% while images are waiting; near 0% when the backlog is empty',
    check: 'near 0% while images are waiting and segmentation is not paused: the worker is stuck',
    now: (s, snap) => { const v = recent(s, 'gpu'); return v == null ? null : `${nf(v, 0)}%` },
    ok: (s, snap) => {
      const v = recent(s, 'gpu')
      if (v == null) return null
      return !(snap?.backlog > 0 && v < 5)
    },
  },
  {
    what: 'GPU temperature',
    normal: '55–75 °C under load (measured peak 73 °C)',
    check: 'above 83 °C for minutes: check the fans and the room',
    now: (s) => { const v = recent(s, 'gpuTemp'); return v == null ? null : `${nf(v, 0)} °C` },
    ok: (s) => { const v = recent(s, 'gpuTemp'); return v == null ? null : v <= 83 },
  },
  {
    what: 'GPU power',
    normal: '400–600 W segmenting (touching the 600 W limit is normal); 80–120 W idle',
    check: 'stuck at idle power while images are waiting',
    now: (s) => { const v = recent(s, 'gpuPower'); return v == null ? null : `${nf(v, 0)} W` },
    ok: (s, snap) => {
      const v = recent(s, 'gpuPower')
      if (v == null) return null
      return !(snap?.backlog > 0 && v < 150)
    },
  },
  {
    what: 'GPU memory',
    normal: '≈5.3 GB per Cellpose process: 5.3 GB with only the station, ≈10.7 GB with the standalone script too',
    check: 'above 28 of 32 GB, or "CUDA OOM" in the header',
    now: (s) => { const v = recent(s, 'vramUsed', 1); return v == null ? null : `${nf(v, 1)} GB` },
    ok: (s, snap) => {
      const v = recent(s, 'vramUsed', 1)
      if (v == null) return null
      return v <= 28 && !(snap?.worker?.oom_events > 0)
    },
  },
  {
    what: 'CPU',
    normal: '10–25% (brief spikes to ~90% at startup while it reads existing images)',
    check: 'above 80% for minutes',
    now: (s) => { const v = recent(s, 'cpu'); return v == null ? null : `${nf(v, 0)}%` },
    ok: (s) => { const v = recent(s, 'cpu'); return v == null ? null : v <= 80 },
  },
  {
    what: 'RAM',
    normal: '≈20% (12 of 62 GB)',
    check: 'above 80%',
    now: (s) => { const v = recent(s, 'ram'); return v == null ? null : `${nf(v, 0)}%` },
    ok: (s) => { const v = recent(s, 'ram'); return v == null ? null : v <= 80 },
  },
  {
    what: 'Disk writes',
    normal: '5–50 MB/s during acquisition (images plus masks); reads ≈0 (served from memory)',
    check: 'free space under 50 GB (an alert also fires)',
    now: (s, snap) => {
      const v = recent(s, 'diskWrite')
      return v == null ? null : `${nf(v, 0)} MB/s · ${bytes(snap?.disk_free_bytes)} free`
    },
    ok: (s, snap) => (snap?.disk_free_bytes == null ? null : snap.disk_free_bytes >= 50e9),
  },
  {
    what: 'Time between images',
    normal: '≈0.6 s mean (PC every ~0.47 s, Green every ~0.70 s); longer pauses at well moves',
    check: 'mean above 1 s for minutes during a z-stack: the microscope has slowed down',
    now: (s, snap) => (snap?.inter_arrival_mean_s == null ? null
      : `${nf(snap.inter_arrival_mean_s, 2)} s`),
    ok: (s, snap) => (snap?.inter_arrival_mean_s == null ? null
      : snap.inter_arrival_mean_s <= 1.0),
  },
  {
    what: 'Segmentation speed',
    normal: '≈0.9 img/s while the standalone script shares the GPU; the microscope makes ≈1.65 img/s, so the backlog grows during a plate and drains after',
    check: '0 img/s while images are waiting and nothing is paused',
    now: (s) => { const v = recent(s, 'rate'); return v == null ? null : `${nf(v, 2)} img/s` },
    ok: (s, snap) => {
      const v = recent(s, 'rate')
      if (v == null) return null
      return !(snap?.backlog > 0 && v === 0 && !snap?.global_pause)
    },
  },
]

export default function Reference({ series, snap }) {
  return (
    <div className="card">
      <div className="card-header">
        <h3 className="card-title">What normal looks like</h3>
        <span className="chart-note">this workstation · live values averaged over 30 s</span>
      </div>
      <div className="card-body">
        <table className="data reference">
          <thead>
            <tr><th>What</th><th>Normal</th><th>Now</th><th>Status</th><th>Look into it when</th></tr>
          </thead>
          <tbody>
            {ROWS.map((r) => {
              const now = r.now(series, snap)
              const ok = now == null ? null : r.ok(series, snap)
              return (
                <tr key={r.what}>
                  <td>{r.what}</td>
                  <td className="muted">{r.normal}</td>
                  <td className="num">{now ?? '—'}</td>
                  <td>
                    <span className={`ref-status ${ok == null ? 'state-idle' : ok ? 'state-ok' : 'state-warn'}`}>
                      {ok == null ? '—' : ok ? 'normal' : 'check'}
                    </span>
                  </td>
                  <td className="muted">{r.check}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="chart-note" style={{ marginBottom: 0 }}>
          Measured on this machine during a full-plate run (96 wells, PC + Green, 20 Z). A
          different protocol, objective or second program on the GPU moves these numbers.
          Details in <span className="num">docs/WORKSTATION.md</span>.
        </p>
      </div>
    </div>
  )
}
