import http from '../../../core/httpClient.js'

const base = '/platform/yt-dlp'

export async function listCookies(site) {
  const res = await http.get(`${base}/cookies`, { params: site ? { site } : undefined })
  return res?.data ?? []
}

export async function saveCookies(payload) {
  const res = await http.post(`${base}/cookies`, payload)
  return res?.data ?? res
}

export async function updateCookieActivation(id, isActive) {
  const res = await http.patch(`${base}/cookies/${id}`, { is_active: !!isActive })
  return res?.data ?? res
}

export async function deleteCookie(id) {
  const res = await http.delete(`${base}/cookies/${id}`)
  return res?.data ?? res
}

export async function getBrowserOverview() {
  const res = await http.get(`${base}/browser-overview`)
  return res?.data ?? {}
}

export async function startBrowserSession(payload) {
  const res = await http.post(`${base}/browser-sessions`, payload)
  return res?.data ?? res
}

export async function getBrowserSession(id) {
  const res = await http.get(`${base}/browser-sessions/${encodeURIComponent(id)}`)
  return res?.data ?? res
}

export async function cancelBrowserSession(id) {
  const res = await http.post(`${base}/browser-sessions/${encodeURIComponent(id)}/cancel`)
  return res?.data ?? res
}

export const SITE_OPTIONS = [
  { value: 'tiktok', label: 'TikTok' },
  { value: 'douyin', label: '抖音' },
  { value: 'youtube', label: 'YouTube' },
  { value: 'kuaishou', label: '快手 / Kwai' },
  { value: 'facebook', label: 'Facebook' },
  { value: 'instagram', label: 'Instagram' },
  { value: 'twitter', label: 'X / Twitter' },
  { value: 'bilibili', label: '哔哩哔哩' },
  { value: 'xiaohongshu', label: '小红书' },
  { value: 'weibo', label: '微博' },
  { value: 'vimeo', label: 'Vimeo' },
  { value: 'reddit', label: 'Reddit' },
  { value: 'twitch', label: 'Twitch' },
  { value: 'dailymotion', label: 'Dailymotion' },
  { value: 'pinterest', label: 'Pinterest' },
  { value: 'linkedin', label: 'LinkedIn' },
  { value: 'nicovideo', label: 'NicoNico' },
  { value: 'youku', label: '优酷' },
  { value: 'iqiyi', label: '爱奇艺 / iQIYI' },
]

export function siteLabel(site) {
  return SITE_OPTIONS.find((s) => s.value === site)?.label || site || '未知站点'
}

export default {
  listCookies,
  saveCookies,
  updateCookieActivation,
  deleteCookie,
  getBrowserOverview,
  startBrowserSession,
  getBrowserSession,
  cancelBrowserSession,
  siteLabel,
  SITE_OPTIONS,
}
