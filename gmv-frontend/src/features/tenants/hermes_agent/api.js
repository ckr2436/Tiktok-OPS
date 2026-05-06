import http from '../../../lib/http.js'

function basePath(wid) {
  if (!wid) throw new Error('workspace_id (wid) is required')
  return `/tenants/${encodeURIComponent(wid)}/hermes-agent`
}

export async function fetchHermesPermissions(wid) {
  const res = await http.get(`${basePath(wid)}/permissions`)
  const data = res.data

  if (Array.isArray(data)) return data
  if (Array.isArray(data?.permissions)) return data.permissions
  if (Array.isArray(data?.items)) {
    return data.items
      .filter((item) => item?.is_enabled !== false && typeof item?.feature_key === 'string')
      .map((item) => item.feature_key)
  }
  return []
}

export async function postHermesAgent(wid, endpoint, payload) {
  const res = await http.post(`${basePath(wid)}/${endpoint}`, payload)
  return res.data
}

