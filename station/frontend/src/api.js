// The station has its own token, under its own key: the two apps authenticate
// separately and one must not silently sign you into the other.
const TOKEN_KEY = 'st_token'

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
    const detail = await res.json().then((d) => d.detail).catch(() => null)
    throw new Error(detail
      ? (typeof detail === 'string' ? detail : JSON.stringify(detail))
      : `${res.status} ${res.statusText}`)
  }
  return res.json()
}

export const getJSON = (path) => fetch(path).then((r) => r.json())
