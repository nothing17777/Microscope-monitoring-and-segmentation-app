export const nf = (n, d = 0) =>
  n === null || n === undefined || Number.isNaN(n) ? '—' : Number(n).toFixed(d)

// 1.07 img/s, never 1.0714285. Precision tracks magnitude.
export function rate(r) {
  if (r === null || r === undefined || !Number.isFinite(r)) return '—'
  if (r === 0) return '0'
  if (r >= 100) return r.toFixed(0)
  if (r >= 10) return r.toFixed(1)
  return r.toFixed(2)
}

export function bytes(b) {
  if (b === null || b === undefined) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0; let v = Number(b)
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1 }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`
}

export function dur(s) {
  if (s === null || s === undefined || !Number.isFinite(s)) return '—'
  const t = Math.max(0, Math.round(s))
  const h = Math.floor(t / 3600); const m = Math.floor((t % 3600) / 60); const sec = t % 60
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m) return `${m}m ${String(sec).padStart(2, '0')}s`
  return `${sec}s`
}

// A range, because a single number implies a precision we do not have.
export function durRange(low, high) {
  if (low === null || low === undefined || high === null || high === undefined) return '—'
  const a = dur(low); const b = dur(high)
  return a === b ? a : `${a} – ${b}`
}

export const clock = (ts) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

export const int = (n) =>
  n === null || n === undefined ? '—' : Number(n).toLocaleString()
