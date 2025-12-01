import http from '../../../core/httpClient.js'

const base = '/platform/yt-dlp'

export async function listCookies(site) {
  const res = await http.get(`${base}/cookies`, { params: site ? { site } : undefined })
  return res?.data ?? []
}

export async function updateCookieActivation(id, isActive) {
  const res = await http.patch(`${base}/cookies/${id}`, { is_active: !!isActive })
  return res?.data ?? res
}

export async function createLoginSession(payload) {
  const res = await http.post(`${base}/login-sessions`, payload, { timeout: 60000 })
  return res?.data ?? res
}

export async function getLoginSession(sessionId) {
  const res = await http.get(`${base}/login-sessions/${sessionId}`)
  return res?.data ?? res
}

export const SITE_OPTIONS = [
  { value: 'tiktok', label: 'TikTok' },
  { value: 'douyin', label: '抖音' },
  { value: 'youtube', label: 'YouTube' },
]

export function siteLabel(site) {
  return SITE_OPTIONS.find((s) => s.value === site)?.label || site || '未知站点'
}

export default {
  listCookies,
  updateCookieActivation,
  createLoginSession,
  getLoginSession,
  siteLabel,
  SITE_OPTIONS,
}
