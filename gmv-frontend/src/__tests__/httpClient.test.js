import { describe, it, expect, beforeEach, afterEach } from './testUtils.js'
import http from '../core/httpClient.js'
import httpReducer, { recordHeaders } from '../store/httpSlice.js'

describe('http client interceptors', () => {
  let originalAdapter
  let capturedConfig

  beforeEach(() => {
    originalAdapter = http.defaults.adapter
    capturedConfig = undefined
    http.defaults.adapter = async (config) => {
      capturedConfig = config
      return {
        data: {},
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      }
    }
  })

  afterEach(() => {
    http.defaults.adapter = originalAdapter
  })

  it('injects Idempotency-Key on write requests', async () => {
    await http.post('/test-idempotency', { foo: 'bar' })
    expect(capturedConfig?.headers?.['Idempotency-Key']).toBeTruthy()
  })
})

describe('httpSlice header parsing', () => {
  it('records rate limit headers and request id', () => {
    const initial = httpReducer(undefined, { type: 'init' })
    const headers = {
      'retry-after': '12',
      'x-next-allowed-at': '2025-01-01T00:00:00Z',
      'x-ratelimit-limit': '10',
      'x-ratelimit-remaining': '7',
      'x-ratelimit-reset': '42',
      'x-request-id': 'req-123',
    }
    const next = httpReducer(initial, recordHeaders({ headers, status: 429, method: 'post', url: '/demo' }))
    expect(next.retryAfterSec).toBe(12)
    expect(next.nextAllowedAt).toBe('2025-01-01T00:00:00.000Z')
    expect(next.rateLimit).toEqual({ limit: 10, remaining: 7, reset: 42 })
    expect(next.lastRequestId).toBe('req-123')
    expect(next.lastStatus).toBe(429)
    expect(next.method).toBe('post')
    expect(next.lastUrl).toBe('/demo')
  })
})

