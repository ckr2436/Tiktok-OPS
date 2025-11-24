// src/features/platform/email/service.js
import http from '../../../core/httpClient'

export async function fetchEmailSettings() {
  const res = await http.get('/platform/email/settings')
  return res?.data ?? res
}

export async function saveEmailSettings(payload) {
  const res = await http.put('/platform/email/settings', payload)
  return res?.data ?? res
}

export async function sendTestEmail(to_email) {
  const res = await http.post('/platform/email/test', { to_email })
  return res?.data ?? res
}

export default {
  fetchEmailSettings,
  saveEmailSettings,
  sendTestEmail,
}
