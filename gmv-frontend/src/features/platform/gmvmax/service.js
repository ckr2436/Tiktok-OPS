// src/features/platform/gmvmax/service.js
import http from '../../../core/httpClient'

const basePath = '/admin/platform/gmvmax/monitoring-strategies'

export async function listMonitoringStrategies(params = {}) {
  const res = await http.get(basePath, { params })
  return res?.data ?? res
}

export async function createMonitoringStrategy(payload) {
  const res = await http.post(basePath, payload)
  return res?.data ?? res
}

export async function updateMonitoringStrategy(id, payload) {
  const res = await http.patch(`${basePath}/${id}`, payload)
  return res?.data ?? res
}

export async function deleteMonitoringStrategy(id) {
  const res = await http.delete(`${basePath}/${id}`)
  return res?.data ?? res
}

export async function enableMonitoringStrategy(id) {
  const res = await http.post(`${basePath}/${id}/enable`)
  return res?.data ?? res
}

export async function disableMonitoringStrategy(id) {
  const res = await http.post(`${basePath}/${id}/disable`)
  return res?.data ?? res
}

export default {
  listMonitoringStrategies,
  createMonitoringStrategy,
  updateMonitoringStrategy,
  deleteMonitoringStrategy,
  enableMonitoringStrategy,
  disableMonitoringStrategy,
}
