// State-changing calls carry the shared token; read-only ones never need it.
// The token is kept in localStorage so a phone only types it once.

const TOKEN_KEY = 'mm_token'

export const getToken = () => {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}

export const setToken = (t) => {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY) } catch { /* private mode */ }
}

export async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': getToken() },
    body: JSON.stringify(body ?? {}),
  })
  if (!res.ok) {
    // FastAPI puts the reason in `detail`; surfacing it beats "request failed".
    const detail = await res.json().then((d) => d.detail).catch(() => null)
    throw new Error(
      detail
        ? (typeof detail === 'string' ? detail : JSON.stringify(detail))
        : `${res.status} ${res.statusText}`,
    )
  }
  return res.json()
}

export const getJSON = (path) => fetch(path).then((r) => r.json())
