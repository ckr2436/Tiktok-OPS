import { beforeEach, describe, expect, it, vi } from 'vitest'
import http from '../../../core/httpClient.js'
import flowAccountApi from './service.js'

vi.mock('../../../core/httpClient.js', () => ({ default: {
  get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
} }))

describe('flowAccountApi', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    http.get.mockResolvedValue({ data: {} })
    http.post.mockResolvedValue({ data: { success: true } })
    http.patch.mockResolvedValue({ data: { success: true } })
    http.delete.mockResolvedValue({ data: { success: true } })
  })

  it('uses platform-only account pool routes', async () => {
    await flowAccountApi.overview()
    await flowAccountApi.startBrowserSession({ device_id: 'device-a' })
    await flowAccountApi.assignBridgeHost({ target_device_id: 'flow-device', source_workspace_id: 3, source_user_id: 6, source_device_id: 'tenant-device' })
    await flowAccountApi.browserReauth(3, { device_id: 'device-a' })
    await flowAccountApi.updateAccount(3, { video_concurrency: 1 })
    await flowAccountApi.refreshCredits(3)
    await flowAccountApi.refreshAccess(3)
    await flowAccountApi.disable(3)
    await flowAccountApi.remove(3)
    await flowAccountApi.createProxy({ name: 'proxy-a', proxy_url: 'socks5h://127.0.0.1:7893' })
    await flowAccountApi.updateProxy(5, { is_active: false })
    await flowAccountApi.removeProxy(5)

    expect(http.get).toHaveBeenCalledWith('/platform/flow2api/overview', { timeout: 90000 })
    expect(http.post).toHaveBeenCalledWith('/platform/flow2api/browser-sessions', { device_id: 'device-a' }, { timeout: 30000 })
    expect(http.post).toHaveBeenCalledWith('/platform/flow2api/bridge-host-assignments', { target_device_id: 'flow-device', source_workspace_id: 3, source_user_id: 6, source_device_id: 'tenant-device' }, { timeout: 30000 })
    expect(http.post).toHaveBeenCalledWith('/platform/flow2api/tokens/3/browser-reauth', { device_id: 'device-a' }, { timeout: 30000 })
    expect(http.patch).toHaveBeenCalledWith('/platform/flow2api/tokens/3', { video_concurrency: 1 }, { timeout: 90000 })
    expect(http.post).toHaveBeenCalledWith('/platform/flow2api/tokens/3/refresh-credits', null, { timeout: 120000 })
    expect(http.post).toHaveBeenCalledWith('/platform/flow2api/tokens/3/refresh-access', null, { timeout: 180000 })
    expect(http.post).toHaveBeenCalledWith('/platform/flow2api/tokens/3/disable')
    expect(http.delete).toHaveBeenCalledWith('/platform/flow2api/tokens/3')
    expect(http.post).toHaveBeenCalledWith('/platform/flow2api/proxies', { name: 'proxy-a', proxy_url: 'socks5h://127.0.0.1:7893' })
    expect(http.patch).toHaveBeenCalledWith('/platform/flow2api/proxies/5', { is_active: false })
    expect(http.delete).toHaveBeenCalledWith('/platform/flow2api/proxies/5')
  })
})
