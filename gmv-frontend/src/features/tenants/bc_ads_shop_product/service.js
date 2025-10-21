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

export async function triggerManualSync(workspaceId) {
  const res = await http.post(`${BASE_URL}/${workspaceId}/bc_ads_shop_product/sync`)
  return res?.data ?? res
}

export default {
  fetchTenantPlanConfig,
  fetchSyncStatus,
  triggerManualSync,
}
