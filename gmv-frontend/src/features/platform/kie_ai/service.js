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

/**
 * 查询指定 Key 的当前余额（credits）
 * 后端会实时调用 KIE /api/v1/chat/credit
 */
async function getKeyCredit(id) {
  const res = await http.get(`${base}/keys/${id}/credit`)
  return res.data
}

/**
 * 查询“默认 Key”的当前余额（credits）
 */
async function getDefaultKeyCredit() {
  const res = await http.get(`${base}/keys/default/credit`)
  return res.data
}

/**
 * 可选：一次性刷新全部 key 的余额（目前前端没用到，留作扩展）
 */
async function refreshKeyCredit(id) {
  return getKeyCredit(id)
}

const kiePlatformApi = {
  listKeys,
  createKey,
  updateKey,
  deactivateKey,
  getKeyCredit,
  getDefaultKeyCredit,
  refreshKeyCredit,
}

export default kiePlatformApi

