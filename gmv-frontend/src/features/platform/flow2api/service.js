import http from '../../../core/httpClient.js'

const base = '/platform/flow2api'

export const flowAccountApi = {
  async overview() {
    const response = await http.get(`${base}/overview`, { timeout: 90_000 })
    return response.data || {}
  },
  async downloadBridgeAgent({ deviceId, deviceName }) {
    const response = await http.get(`${base}/bridge-agent/download`, {
      params: { device_id: deviceId, device_name: deviceName || undefined },
      responseType: 'blob',
      timeout: 120_000,
    })
    return response.data
  },
  async assignBridgeHost(payload) {
    const response = await http.post(`${base}/bridge-host-assignments`, payload, { timeout: 30_000 })
    return response.data
  },
  async importAccount(payload) {
    const response = await http.post(`${base}/tokens`, payload, { timeout: 120_000 })
    return response.data
  },
  async startBrowserSession(payload) {
    const response = await http.post(`${base}/browser-sessions`, payload, { timeout: 30_000 })
    return response.data
  },
  async browserReauth(id, payload) {
    const response = await http.post(`${base}/tokens/${id}/browser-reauth`, payload, { timeout: 30_000 })
    return response.data
  },
  async cancelBrowserSession(id) {
    const response = await http.post(`${base}/browser-sessions/${encodeURIComponent(id)}/cancel`)
    return response.data
  },
  async updateAccount(id, payload) {
    const response = await http.patch(`${base}/tokens/${id}`, payload, { timeout: 90_000 })
    return response.data
  },
  async enable(id) {
    const response = await http.post(`${base}/tokens/${id}/enable`)
    return response.data
  },
  async disable(id) {
    const response = await http.post(`${base}/tokens/${id}/disable`)
    return response.data
  },
  async refreshCredits(id) {
    const response = await http.post(`${base}/tokens/${id}/refresh-credits`, null, { timeout: 120_000 })
    return response.data
  },
  async refreshAccess(id) {
    const response = await http.post(`${base}/tokens/${id}/refresh-access`, null, { timeout: 180_000 })
    return response.data
  },
  async remove(id) {
    const response = await http.delete(`${base}/tokens/${id}`)
    return response.data
  },
  async createProxy(payload) {
    const response = await http.post(`${base}/proxies`, payload)
    return response.data
  },
  async updateProxy(id, payload) {
    const response = await http.patch(`${base}/proxies/${id}`, payload)
    return response.data
  },
  async removeProxy(id) {
    const response = await http.delete(`${base}/proxies/${id}`)
    return response.data
  },
}

export default flowAccountApi
