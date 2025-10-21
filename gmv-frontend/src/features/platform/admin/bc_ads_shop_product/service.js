// src/features/platform/admin/bc_ads_shop_product/service.js
import http from '../../../../core/httpClient.js'

const BASE_URL = '/platform/admin/bc_ads_shop_product'

export async function fetchPlanConfig() {
  const res = await http.get(`${BASE_URL}/plans`)
  return res?.data ?? res
}

export async function savePlanConfig(payload) {
  const res = await http.put(`${BASE_URL}/plans`, payload)
  return res?.data ?? res
}

export async function publishPlanSnapshot(payload) {
  const res = await http.post(`${BASE_URL}/plans/publish`, payload)
  return res?.data ?? res
}

export default {
  fetchPlanConfig,
  savePlanConfig,
  publishPlanSnapshot,
}
