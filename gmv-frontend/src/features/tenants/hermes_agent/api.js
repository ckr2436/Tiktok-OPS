import http from '../../../lib/http.js'

function basePath(wid) {
  if (!wid) throw new Error('workspace_id (wid) is required')
  return `/tenants/${encodeURIComponent(wid)}/hermes-agent`
}

export async function fetchHermesCapabilities(wid, taskType = 'general') {
  const params = taskType ? { task_type: taskType } : undefined
  const res = await http.get(`${basePath(wid)}/capabilities`, { params })
  return res.data || {}
}

export async function postHermesAgent(wid, endpoint, payload) {
  const res = await http.post(`${basePath(wid)}/${endpoint}`, payload)
  return res.data
}
