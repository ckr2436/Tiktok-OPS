import http from '../../../core/httpClient.js'

const base = '/platform/doubao-lab'

export const doubaoLabApi = {
  async overview() {
    const response = await http.get(`${base}/overview`, { timeout: 30_000 })
    return response.data || {}
  },
  async startBrowserSession(payload) {
    const response = await http.post(`${base}/browser-sessions`, payload, { timeout: 30_000 })
    return response.data
  },
  async cancelBrowserSession(id) {
    const response = await http.post(`${base}/browser-sessions/${encodeURIComponent(id)}/cancel`)
    return response.data
  },
  async verifySession(id) {
    const response = await http.post(`${base}/browser-sessions/${encodeURIComponent(id)}/verify`, null, { timeout: 30_000 })
    return response.data
  },
  async startManualVerification(id) {
    const response = await http.post(
      `${base}/browser-sessions/${encodeURIComponent(id)}/manual-verification/start`,
      null,
      { timeout: 30_000 },
    )
    return response.data
  },
  async completeManualVerification(id) {
    const response = await http.post(
      `${base}/browser-sessions/${encodeURIComponent(id)}/manual-verification/complete`,
      null,
      { timeout: 150_000 },
    )
    return response.data
  },
  async reconcilePool() {
    const response = await http.post(`${base}/pool/reconcile`, null, { timeout: 30_000 })
    return response.data || {}
  },
  async probeSession(id) {
    const response = await http.post(`${base}/browser-sessions/${encodeURIComponent(id)}/probe`, null, { timeout: 30_000 })
    return response.data
  },
  async createTest(id, payload) {
    const response = await http.post(`${base}/browser-sessions/${encodeURIComponent(id)}/tests`, payload, { timeout: 30_000 })
    return response.data
  },
  async updatePoolState(id, enabled) {
    const response = await http.patch(`${base}/browser-sessions/${encodeURIComponent(id)}/pool`, { enabled })
    return response.data
  },
  async updateMembership(id, tier) {
    const response = await http.patch(`${base}/browser-sessions/${encodeURIComponent(id)}/membership`, { tier })
    return response.data
  },
  async relogin(id) {
    const response = await http.post(`${base}/browser-sessions/${encodeURIComponent(id)}/relogin`)
    return response.data
  },
  async updateProxy(id, proxyId) {
    const response = await http.patch(`${base}/browser-sessions/${encodeURIComponent(id)}/proxy`, {
      proxy_id: proxyId == null ? null : Number(proxyId),
    })
    return response.data
  },
  async deleteAccount(id) {
    const response = await http.delete(`${base}/browser-sessions/${encodeURIComponent(id)}`)
    return response.data
  },
}

export default doubaoLabApi
