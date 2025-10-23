import { describe, it, expect } from './testUtils.js'
import { pollLastUntilSettled, DEFAULT_DELAYS } from '../features/tenants/services/syncApi.js'

describe('pollLastUntilSettled', () => {
  it('stops when statuses reach terminal state', async () => {
    let callCount = 0
    const mock = async () => {
      callCount += 1
      if (callCount < 2) {
        return { status: 'running' }
      }
      return { status: 'success', summary: { diff: { added: 1, removed: 0, updated: 0 } } }
    }
    const result = await pollLastUntilSettled({
      workspaceId: 1,
      provider: 'tiktok-business',
      domain: 'bc',
      authIds: ['1'],
      getLastFn: mock,
      delays: [1, 1, 1],
    })
    expect(result.status).toBe('settled')
    expect(result.attempts).toBeGreaterThanOrEqual(1)
  })

  it('returns timeout when reaching max attempts', async () => {
    const mock = async () => ({ status: 'running' })
    const result = await pollLastUntilSettled({
      workspaceId: 1,
      provider: 'tiktok-business',
      domain: 'bc',
      authIds: ['1'],
      getLastFn: mock,
      delays: [1, 1],
    })
    expect(result.status).toBe('timeout')
    expect(result.attempts).toBe(2)
  })

  it('stops early when failure detected', async () => {
    const result = await pollLastUntilSettled({
      workspaceId: 1,
      provider: 'tiktok-business',
      domain: 'bc',
      authIds: ['1'],
      getLastFn: async () => ({ status: 'failed' }),
      delays: [1, 1, 1],
    })
    expect(result.status).toBe('settled')
    expect(result.attempts).toBe(1)
  })

  it('uses the expected exponential backoff schedule', () => {
    expect(DEFAULT_DELAYS).toEqual([1000, 2000, 4000, 8000, 8000])
  })
})

