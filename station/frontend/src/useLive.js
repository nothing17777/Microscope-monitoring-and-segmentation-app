import { useCallback, useEffect, useRef, useState } from 'react'
import { getJSON } from './api'

// No update for this long and the data on screen is no longer trustworthy.
// People close laptops and come back; say so rather than showing stale
// numbers as though they were live.
const STALE_AFTER_MS = 15000
const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000]

export function useLive() {
  const [snap, setSnap] = useState(null)
  const [connected, setConnected] = useState(false)
  const [stale, setStale] = useState(false)
  const lastAt = useRef(0)
  const attempt = useRef(0)

  useEffect(() => {
    let closed = false
    let sock
    let retry

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      sock = new WebSocket(`${proto}://${location.host}/ws`)
      sock.onopen = () => { attempt.current = 0; setConnected(true) }
      sock.onmessage = (e) => {
        lastAt.current = Date.now()
        setStale(false)
        setSnap(JSON.parse(e.data))
      }
      sock.onclose = () => {
        setConnected(false)
        if (closed) return
        // Exponential backoff, capped: a backend that is down for ten minutes
        // should not be hammered once a second for all of them.
        const wait = BACKOFF_MS[Math.min(attempt.current, BACKOFF_MS.length - 1)]
        attempt.current += 1
        retry = setTimeout(connect, wait)
      }
      sock.onerror = () => sock.close()
    }
    connect()

    const tick = setInterval(() => {
      setStale(lastAt.current > 0 && Date.now() - lastAt.current > STALE_AFTER_MS)
    }, 1000)

    return () => { closed = true; clearTimeout(retry); clearInterval(tick); sock?.close() }
  }, [])

  // Fill the page before the first frame arrives, so a reload is never blank.
  useEffect(() => {
    if (snap) return
    getJSON('/api/status').then((d) => { lastAt.current = Date.now(); setSnap(d) }).catch(() => {})
  }, [snap])

  const refresh = useCallback(() => {
    getJSON('/api/status').then((d) => { lastAt.current = Date.now(); setSnap(d) }).catch(() => {})
  }, [])

  return { snap, connected, stale, refresh, secondsSince: lastAt.current ? (Date.now() - lastAt.current) / 1000 : null }
}

export function usePoll(url, intervalMs) {
  const [data, setData] = useState(null)
  useEffect(() => {
    let alive = true
    // A null url means "nothing to poll yet" (no active run, say) rather than
    // an error; without this it would fetch the string "null" every tick.
    if (!url) { setData(null); return () => { alive = false } }
    const go = () => getJSON(url).then((d) => alive && setData(d)).catch(() => {})
    go()
    if (!intervalMs) return () => { alive = false }
    const id = setInterval(go, intervalMs)
    return () => { alive = false; clearInterval(id) }
  }, [url, intervalMs])
  return data
}
