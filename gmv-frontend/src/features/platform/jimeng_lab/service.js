import http from '../../../core/httpClient.js'

const base = '/platform/jimeng-lab'

export const jimengLabApi = {
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
    const response = await http.post(`${base}/browser-sessions/${encodeURIComponent(id)}/verify`, null, { timeout: 60_000 })
    return response.data
  },
  async createTest(id, payload) {
    const response = await http.post(`${base}/browser-sessions/${encodeURIComponent(id)}/tests`, payload, { timeout: 30_000 })
    return response.data
  },
}

export default jimengLabApi
