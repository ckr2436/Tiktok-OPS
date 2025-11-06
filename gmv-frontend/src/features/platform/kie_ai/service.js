// src/features/platform/kie_ai/service.js
import http from '../../../core/httpClient.js'

/**
 * 平台侧 KIE API key 管理相关 API 封装
 */
const base = '/platform/kie-ai'

async function listKeys() {
  const res = await http.get(`${base}/keys`)
  return res.data || []
}

async function createKey(payload) {
  const res = await http.post(`${base}/keys`, payload)
  return res.data
}

async function updateKey(id, payload) {
  const res = await http.patch(`${base}/keys/${id}`, payload)
  return res.data
}

async function deactivateKey(id) {
  const res = await http.delete(`${base}/keys/${id}`)
  return res.data
}

async function getKeyCredit(id) {
  const res = await http.get(`${base}/keys/${id}/credit`)
  return res.data
}

async function getDefaultKeyCredit() {
  const res = await http.get(`${base}/keys/default/credit`)
  return res.data
}

const kiePlatformApi = {
  listKeys,
  createKey,
  updateKey,
  deactivateKey,
  getKeyCredit,
  getDefaultKeyCredit,
}

export default kiePlatformApi

