import React, { useState } from 'react'
import { post } from './api'
import { durRange, int, dur } from './format'

const STATE_CLASS = {
  running: 'state-ok',
  queued: 'state-idle',
  paused: 'state-warn',
  done: 'state-ok',
  failed: 'state-bad',
  cancelled: 'state-idle',
}

function Confidence({ job }) {
  if (job.eta_confidence === 'none') {
    // Never show a number we cannot stand behind.
    return <span className="muted">ETA {job.eta_note}</span>
  }
  return (
    <span>
      ETA <span className="num">{durRange(job.eta_low, job.eta_high)}</span>
      {job.eta_confidence === 'low' ? <span className="muted"> (rough)</span> : null}
    </span>
  )
}

function Job({ job, onAction, busy, controlsDisabled, disabledReason }) {
  const running = job.status === 'running'
  const pct = job.total ? Math.min(100, (job.completed / job.total) * 100) : 0
  const act = (a) => onAction(job.id, a)

  return (
    <div className={`job ${running ? 'job-running' : ''} ${STATE_CLASS[job.status] || 'state-idle'}`}>
      <div className="job-pos num">{running ? '▸' : `#${(job.position ?? 0) + 1}`}</div>
      <div className="job-main">
        <div className="job-title">
          <span className="job-name">{job.label}</span>
          <span className="status-chip">{job.status}</span>
        </div>
        <div className="job-meta">
          <span className="num">{int(job.completed)} / {int(job.total)}</span>
          {job.failed ? <span style={{ color: 'var(--red)' }}>{int(job.failed)} failed</span> : null}
          {running || job.status === 'queued' ? <Confidence job={job} /> : null}
          {job.status === 'queued' && job.starts_in_seconds != null
            ? <span className="muted">starts in <span className="num">{dur(job.starts_in_seconds)}</span></span>
            : null}
          {job.status === 'paused'
            ? <span style={{ color: 'var(--yellow)' }}>backlog growing</span>
            : null}
        </div>
        <div className="job-bar"><span style={{ width: `${pct}%` }} /></div>
      </div>
      <div className="job-actions">
        {controlsDisabled ? (
          <span className="muted" title={disabledReason} style={{ fontSize: 12 }}>no control</span>
        ) : (
          <>
            {job.status === 'running' || job.status === 'queued' ? (
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => act('pause')}>Pause</button>
            ) : null}
            {job.status === 'paused' ? (
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => act('resume')}>Resume</button>
            ) : null}
            <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => act('cancel')}>Cancel</button>
          </>
        )}
      </div>
    </div>
  )
}

function Lane({ title, lane, emptyTitle, emptyBody, controlsDisabled, disabledReason,
                onAction, busy, header }) {
  return (
    <div className="card queue-lane">
      <div className="card-header">
        <h3 className="card-title">{title}</h3>
        {header}
      </div>
      {lane.jobs.length === 0 ? (
        <div className="queue-empty">
          <strong>{emptyTitle}</strong>
          {emptyBody}
        </div>
      ) : (
        lane.jobs.map((j) => (
          <Job key={j.id} job={j} onAction={onAction} busy={busy}
               controlsDisabled={controlsDisabled} disabledReason={disabledReason} />
        ))
      )}
    </div>
  )
}

function NewAcquisition({ disabled, reason, onCreated }) {
  const [plate, setPlate] = useState('')
  const [expected, setExpected] = useState('')
  const [notes, setNotes] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setError(null)
    try {
      await post('/api/jobs', {
        plate: plate.trim(),
        expected_images: Number(expected),
        notes: notes.trim(),
      })
      setPlate(''); setExpected(''); setNotes('')
      onCreated()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="inline-form" onSubmit={submit} style={{ padding: 'var(--space-4)' }}>
      <div className="field" style={{ flex: '2 1 10rem' }}>
        <label htmlFor="plate">Plate</label>
        <input id="plate" value={plate} required maxLength={120}
               placeholder="MSC-96-A" onChange={(e) => setPlate(e.target.value)} />
      </div>
      <div className="field" style={{ flex: '1 1 8rem' }}>
        <label htmlFor="expected">Expected images</label>
        <input id="expected" value={expected} required type="number" min="1"
               placeholder="15360" onChange={(e) => setExpected(e.target.value)} />
      </div>
      <div className="field" style={{ flex: '2 1 10rem' }}>
        <label htmlFor="notes">Notes</label>
        <input id="notes" value={notes} maxLength={2000}
               placeholder="2x2, 20 slices, phase + green"
               onChange={(e) => setNotes(e.target.value)} />
      </div>
      <button className="btn btn-primary" disabled={busy || disabled} title={disabled ? reason : undefined}>
        {busy ? 'Adding…' : 'Track run'}
      </button>
      {error ? <div className="form-error" style={{ flex: '1 1 100%' }}>{error}</div> : null}
    </form>
  )
}

export default function Queue({ queue, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  if (!queue) return null

  const { adapter } = queue
  const observed = !adapter.can_control

  const action = async (id, a) => {
    setBusy(true); setError(null)
    try { await post(`/api/jobs/${id}/${a}`) ; onChanged() }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  const toggleGlobal = async () => {
    setBusy(true); setError(null)
    try { await post('/api/segmentation/pause', { paused: !queue.global_pause }); onChanged() }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  return (
    <div className="stack">
      {error ? (
        <div className="notice state-bad"><strong>Could not do that.</strong> {error}</div>
      ) : null}

      <div className="row col-2">
        <Lane
          title="Acquisition"
          lane={queue.acquisition}
          controlsDisabled={observed}
          disabledReason={adapter.reason}
          onAction={action}
          busy={busy}
          emptyTitle="No run being tracked"
          emptyBody={observed
            ? 'Start the run in LumaViewPro, then add it below so progress and ETA can be followed here.'
            : 'Add a run below to start it.'}
          header={<span className="muted" style={{ fontSize: 12 }}>
            {observed ? 'watching files — no control' : 'control enabled (stubbed)'}
          </span>}
        />

        <Lane
          title="Segmentation"
          lane={queue.segmentation}
          controlsDisabled={false}
          onAction={action}
          busy={busy}
          emptyTitle="Nothing queued"
          emptyBody="Segmentation jobs appear by themselves as files land in the input folder."
          header={(
            <button className="btn btn-ghost btn-sm" disabled={busy} onClick={toggleGlobal}>
              {queue.global_pause ? 'Resume worker' : 'Pause worker'}
            </button>
          )}
        />
      </div>

      <div className="card">
        <div className="card-header">
          <h3 className="card-title">Track an acquisition run</h3>
          {observed ? (
            <span className="muted" style={{ fontSize: 12 }}>{adapter.reason}</span>
          ) : null}
        </div>
        <NewAcquisition disabled={false} onCreated={onChanged} />
      </div>
    </div>
  )
}
