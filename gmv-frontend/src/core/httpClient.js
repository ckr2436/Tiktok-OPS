import axios from 'axios'
import { apiRoot } from './config.js'
import { store } from '../app/store.js'
import { recordHeaders } from '../store/httpSlice.js'

export const http = axios.create({
  baseURL: apiRoot,
  withCredentials: true,
  timeout: 15000,
})

function makeIdempotencyKey() {
  const g = globalThis || {}
  const cryptoObj = g.crypto || {}
  if (typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID()
  }
  if (typeof cryptoObj.getRandomValues === 'function') {
    const bytes = new Uint8Array(16)
    cryptoObj.getRandomValues(bytes)
    return Array.from(bytes)
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('')
  }
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

function dispatchHeaders(response) {
  if (!response) return
  try {
    const { status, headers, config } = response
    const method = config?.method ?? null
    const url = config?.url ?? null
    store.dispatch(recordHeaders({ status, headers, method, url }))
  } catch (err) {
    // 静默处理派发错误，避免影响请求链路
  }
}

http.interceptors.request.use((config) => {
  const method = String(config?.method || '').toLowerCase()
  if (['post', 'put', 'patch', 'delete'].includes(method)) {
    if (!config.headers) config.headers = {}
    const headers = config.headers
    const existing = headers['Idempotency-Key'] || headers['idempotency-key']
    if (!existing) {
      headers['Idempotency-Key'] = makeIdempotencyKey()
    }
  }
  return config
})

http.interceptors.response.use(
  (res) => {
    dispatchHeaders(res)
    return res
  },
  (err) => {
    const payload = err?.response?.data
    const status = err?.response?.status
    dispatchHeaders(err?.response)
    const message =
      payload?.error?.message ||
      payload?.detail ||
      err?.message ||
      '网络错误，请稍后再试'
    const e = new Error(message)
    e.status = status
    e.payload = payload
    e.headers = err?.response?.headers || {}
    return Promise.reject(e)
  }
)

export default http

