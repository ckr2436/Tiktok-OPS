import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../../core/httpClient.js', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}))

import http from '../../../core/httpClient.js'
import jimengLabApi from './service.js'

describe('jimengLabApi', () => {
  beforeEach(() => vi.clearAllMocks())

  it('uses platform-admin-only JiMeng lab routes', async () => {
    http.get.mockResolvedValue({ data: {} })
    http.post.mockResolvedValue({ data: {} })
    await jimengLabApi.overview()
    await jimengLabApi.startBrowserSession({ device_id: 'dev-a', proxy_id: 2 })
    await jimengLabApi.verifySession('jimeng-a')
    await jimengLabApi.createTest('jimeng-a', { prompt: 'test', model: 'jimeng-video-seedance-2.0-fast' })
    await jimengLabApi.cancelBrowserSession('jimeng-a')
    expect(http.get).toHaveBeenCalledWith('/platform/jimeng-lab/overview', { timeout: 30000 })
    expect(http.post).toHaveBeenCalledWith('/platform/jimeng-lab/browser-sessions', { device_id: 'dev-a', proxy_id: 2 }, { timeout: 30000 })
    expect(http.post).toHaveBeenCalledWith('/platform/jimeng-lab/browser-sessions/jimeng-a/verify', null, { timeout: 60000 })
    expect(http.post).toHaveBeenCalledWith('/platform/jimeng-lab/browser-sessions/jimeng-a/tests', { prompt: 'test', model: 'jimeng-video-seedance-2.0-fast' }, { timeout: 30000 })
    expect(http.post).toHaveBeenCalledWith('/platform/jimeng-lab/browser-sessions/jimeng-a/cancel')
  })
})
