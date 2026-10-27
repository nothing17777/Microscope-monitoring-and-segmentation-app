import React, { useState } from 'react'
import { post } from './api'
import { dur, durRange, int } from './format'

const CHIP = {
  running: 'state-ok', queued: 'state-idle', paused: 'state-warn',
  done: 'state-ok', failed: 'state-bad', cancelled: 'state-idle',
}

function Eta({ eta, prefix = 'ETA' }) {
  if (!eta || eta.eta_confidence === 'none') {
    return <span className="muted">{prefix} {eta?.eta_note ?? 'unknown'}</span>
  }
  return (
    <span>
      {prefix} <span className="num">{durRange(eta.eta_low, eta.eta_high)}</span>
      {eta.eta_confidence === 'low' ? <span className="muted"> (rough)</span> : null}
    </span>
  )
}

function Row({ run, kind, onAction, busy, canControl, reason }) {
  const running = run.status === 'running'
  const acq = kind === 'acquisition'
  const total = acq ? run.expected_image_count : run.found
  const value = acq ? run.captured : run.segmented
  const pct = total ? Math.min(100, (value / total) * 100) : 0
  const segPaused = run.seg_status === 'paused'

  return (
    <div className={`job ${running ? 'job-running' : ''} ${CHIP[run.status] || 'state-idle'}`}>
      <div className="job-pos num">{running ? '▸' : `#${(run.position ?? 0) + 1}`}</div>
      <div className="job-main">
        <div className="job-title">
          <a className="job-name" href={`#/runs/${run.id}`}>{run.name}</a>
          <span className="status-chip">{acq ? run.status : (segPaused ? 'paused' : 'active')}</span>
        </div>
        <div className="job-meta">
          <span className="num">{int(value)} / {int(total)}</span>
          {acq ? <span>{int(run.wells_seen)} well(s)</span> : null}
          {run.failed ? <span style={{ color: 'var(--red)' }}>{int(run.failed)} failed</span> : null}
          <Eta eta={acq ? run.acquisition_eta : run.segmentation_eta} />
          {run.status === 'queued' && run.starts_in_seconds != null
            ? <span className="muted">starts in <span className="num">{dur(run.starts_in_seconds)}</span></span>
            : null}
          {!acq && segPaused ? <span style={{ color: 'var(--yellow)' }}>backlog growing</span> : null}
        </div>
        <div className="job-bar"><span style={{ width: `${pct}%` }} /></div>
      </div>
      <div className="job-actions">
        {acq ? (
          // Hidden, not disabled-and-broken: in Observed mode there is
          // nothing these buttons could do, so they are not rendered.
          canControl ? (
            <>
              {running ? <button className="btn btn-ghost btn-sm" disabled={busy}
                                  onClick={() => onAction(run.id, 'pause')}>Pause</button> : null}
              {run.status === 'paused' ? <button className="btn btn-ghost btn-sm" disabled={busy}
                                  onClick={() => onAction(run.id, 'resume')}>Resume</button> : null}
              <button className="btn btn-ghost btn-sm" disabled={busy}
                      onClick={() => onAction(run.id, 'cancel')}>Cancel</button>
            </>
          ) : (
            <span className="muted" style={{ fontSize: 12, maxWidth: '14rem' }}>{reason}</span>
          )
        ) : (
          <>
            <button className="btn btn-ghost btn-sm" disabled={busy}
                    onClick={() => onAction(run.id, segPaused ? 'seg-resume' : 'seg-pause')}>
              {segPaused ? 'Resume' : 'Pause'}
            </button>
            <button className="btn btn-ghost btn-sm" disabled={busy}
                    onClick={() => onAction(run.id, 'cancel')}>Cancel</button>
          </>
        )}
      </div>
    </div>
  )
}

function Lane({ title, kind, runs, header, emptyTitle, emptyBody, ...rest }) {
  return (
    <div className="card queue-lane">
      <div className="card-header">
        <h3 className="card-title">{title}</h3>
        {header}
      </div>
      {runs.length === 0 ? (
        <div className="queue-empty"><strong>{emptyTitle}</strong>{emptyBody}</div>
      ) : runs.map((r) => <Row key={r.id} run={r} kind={kind} {...rest} />)}
    </div>
  )
}

export default function Queue({ queue, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  if (!queue) return null
  const canControl = queue.adapter.can_control

  const action = async (id, a) => {
    setBusy(true); setError(null)
    try {
      if (a === 'seg-pause' || a === 'seg-resume') {
        await post(`/api/runs/${id}/segmentation/pause`, { paused: a === 'seg-pause' })
      } else {
        await post(`/api/runs/${id}/${a}`)
      }
      onChanged()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const toggleGlobal = async () => {
    setBusy(true); setError(null)
    try { await post('/api/segmentation/pause', { paused: !queue.global_pause }); onChanged() }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const common = { onAction: action, busy, canControl, reason: queue.adapter.reason }

  return (
    <div className="stack">
      {error ? <div className="notice state-bad"><strong>Could not do that.</strong> {error}</div> : null}
      <div className="row col-2">
        <Lane title="Acquisition queue" kind="acquisition" runs={queue.acquisition}
              emptyTitle="No run in progress"
              emptyBody={canControl ? 'Start one below.'
                : 'Start the run in LumaViewPro, then add it below so it can be followed here.'}
              header={<span className="muted" style={{ fontSize: 12 }}>
                {canControl ? 'control enabled (stubbed)' : 'watching files — no control'}</span>}
              {...common} />
        <Lane title="Segmentation queue" kind="segmentation" runs={queue.segmentation}
              emptyTitle="Nothing to segment"
              emptyBody="Work appears by itself as images land in the input folder."
              header={<button className="btn btn-ghost btn-sm" disabled={busy} onClick={toggleGlobal}>
                {queue.global_pause ? 'Resume worker' : 'Pause worker'}</button>}
              {...common} />
      </div>
    </div>
  )
}
