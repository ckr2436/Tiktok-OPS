// src/features/tenants/bc_ads_shop_product/service.js
import http from '../../../core/httpClient.js'

const BASE_URL = '/tenants'

export async function fetchTenantPlanConfig(workspaceId) {
  const res = await http.get(`${BASE_URL}/${workspaceId}/bc_ads_shop_product/plans`)
  return res?.data ?? res
}

export async function fetchSyncStatus(workspaceId) {
  const res = await http.get(`${BASE_URL}/${workspaceId}/bc_ads_shop_product/sync-status`)
  return res?.data ?? res
}

export async function fetchBindingSummary(workspaceId) {
  const res = await http.get(`${BASE_URL}/${workspaceId}/bc_ads_shop_product/bindings`)
  return res?.data ?? res
}

export async function triggerManualSync(workspaceId, bindingId) {
  if (!workspaceId) throw new Error('workspaceId is required')
  const base = `${BASE_URL}/${workspaceId}/bc_ads_shop_product`
  const url = bindingId
    ? `${base}/bindings/${encodeURIComponent(bindingId)}/sync`
    : `${base}/sync`
  const res = await http.post(url)
  return res?.data ?? res
}

export default {
  fetchTenantPlanConfig,
  fetchSyncStatus,
  fetchBindingSummary,
  triggerManualSync,
}
