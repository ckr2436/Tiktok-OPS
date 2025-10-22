import { createSlice } from '@reduxjs/toolkit'

function normalizeHeaders(input) {
  const map = {}
  if (!input) return map
  if (typeof input.forEach === 'function') {
    input.forEach((value, key) => {
      map[String(key).toLowerCase()] = Array.isArray(value) ? value[value.length - 1] : value
    })
    return map
  }
  if (typeof input.entries === 'function') {
    for (const [key, value] of input.entries()) {
      map[String(key).toLowerCase()] = Array.isArray(value) ? value[value.length - 1] : value
    }
    return map
  }
  for (const [key, value] of Object.entries(input)) {
    map[String(key).toLowerCase()] = Array.isArray(value) ? value[value.length - 1] : value
  }
  return map
}

function parseNumber(value) {
  if (value === undefined || value === null || value === '') return null
  const num = Number(value)
  return Number.isFinite(num) ? num : null
}

function parseTimestamp(value) {
  if (!value) return null
  if (value instanceof Date && !Number.isNaN(value.getTime())) {
    return value.toISOString()
  }
  const asNumber = parseNumber(value)
  if (asNumber !== null) {
    if (asNumber > 1e12) {
      return new Date(asNumber).toISOString()
    }
    return new Date(asNumber * 1000).toISOString()
  }
  const dt = new Date(String(value))
  if (!Number.isNaN(dt.getTime())) return dt.toISOString()
  return null
}

const initialState = {
  rateLimit: { limit: null, remaining: null, reset: null },
  nextAllowedAt: null,
  retryAfterSec: null,
  lastRequestId: null,
  lastStatus: null,
  lastUrl: null,
  method: null,
  lastHeaders: {},
}

const slice = createSlice({
  name: 'http',
  initialState,
  reducers: {
    recordHeaders(state, action) {
      const payload = action.payload || {}
      const headers = normalizeHeaders(payload.headers)
      state.lastHeaders = headers
      state.lastStatus = payload.status ?? null
      state.lastUrl = payload.url ?? null
      state.method = payload.method ?? null

      const limit = parseNumber(headers['x-ratelimit-limit'])
      const remaining = parseNumber(headers['x-ratelimit-remaining'])
      const reset = parseNumber(headers['x-ratelimit-reset'])
      state.rateLimit = { limit, remaining, reset }

      state.nextAllowedAt = parseTimestamp(headers['x-next-allowed-at'])
      state.retryAfterSec = parseNumber(headers['retry-after'])

      const reqId = headers['x-request-id'] ?? headers['x-requestid'] ?? headers['x-amzn-requestid']
      if (reqId) state.lastRequestId = String(reqId)
    },
    clearHttpMeta(state) {
      state.rateLimit = { limit: null, remaining: null, reset: null }
      state.nextAllowedAt = null
      state.retryAfterSec = null
      state.lastRequestId = null
      state.lastStatus = null
      state.lastUrl = null
      state.method = null
      state.lastHeaders = {}
    },
  },
})

export const { recordHeaders, clearHttpMeta } = slice.actions
export default slice.reducer

