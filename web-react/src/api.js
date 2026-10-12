const API_BASE = import.meta.env.VITE_API_BASE || ''

async function api(path, opts) {
  const r = await fetch(`${API_BASE}${path}`, opts
    ? { headers: { 'Content-Type': 'application/json' }, ...opts }
    : undefined)
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.detail || r.statusText)
  }
  return r.json()
}

export default api
