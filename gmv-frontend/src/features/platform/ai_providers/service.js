import http from '../../../core/httpClient.js'

const base = '/platform/api-keys'

async function listKeys(params = {}) {
  const res = await http.get(`${base}/keys`, { params })
  return res.data || []
}

async function listModels() {
  const res = await http.get(`${base}/models`)
  return res.data || []
}

async function listProviders() {
  const res = await http.get(`${base}/providers`)
  return res.data || []
}

async function listProviderModels() {
  const res = await http.get(`${base}/provider-models`)
  return res.data || []
}

async function getRoutingOverview(params = {}) {
  const res = await http.get(`${base}/routing-overview`, { params })
  return res.data || {}
}

async function listRoleGroups() {
  const res = await http.get(`${base}/role-groups`)
  return res.data?.items || []
}

async function updateRoleProviderOrder(role, providerOrder) {
  const res = await http.patch(`${base}/role-groups/${encodeURIComponent(role)}/provider-order`, { provider_order: providerOrder })
  return res.data
}

async function listCatalogModels(params = {}) {
  const res = await http.get(`${base}/catalog-models`, { params })
  return res.data || { items: [], page: 1, page_size: 50, total: 0 }
}

async function listRoutes(params = {}) {
  const res = await http.get(`${base}/routes`, { params })
  return res.data || { items: [], page: 1, page_size: 50, total: 0 }
}

async function discoverKey(id) {
  const res = await http.post(`${base}/keys/${id}/discover`)
  return res.data
}

async function discoverAll() {
  const res = await http.post(`${base}/discover-all`)
  return res.data
}

async function updateRoute(id, payload) {
  const res = await http.patch(`${base}/routes/${id}`, payload)
  return res.data
}

async function probeRoute(id, payload = {}) {
  const res = await http.post(`${base}/routes/${id}/probe`, payload)
  return res.data
}

async function resetRouteCircuit(id) {
  const res = await http.post(`${base}/routes/${id}/reset-circuit`)
  return res.data
}

async function updateProviderModel(providerKey, modelId, payload) {
  const res = await http.patch(`${base}/provider-models/${providerKey}/${modelId}`, payload)
  return res.data
}

async function createKey(payload) {
  const res = await http.post(`${base}/keys`, payload)
  return res.data
}

async function updateKey(id, payload) {
  const res = await http.patch(`${base}/keys/${id}`, payload)
  return res.data
}

async function deactivateKey(id) {
  const res = await http.delete(`${base}/keys/${id}`)
  return res.data
}

export default {
  listKeys,
  listModels,
  listProviders,
  listProviderModels,
  getRoutingOverview,
  listRoleGroups,
  updateRoleProviderOrder,
  listCatalogModels,
  listRoutes,
  discoverKey,
  discoverAll,
  updateRoute,
  probeRoute,
  resetRouteCircuit,
  updateProviderModel,
  createKey,
  updateKey,
  deactivateKey,
}
