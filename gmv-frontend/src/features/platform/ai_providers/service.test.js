import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/core/httpClient.js', () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

import http from '@/core/httpClient.js'
import apiKeyApi from './service.js'

describe('platform AI routing service', () => {
  beforeEach(() => {
    for (const method of ['get', 'post', 'patch', 'delete']) http[method].mockReset()
  })

  it('loads the unified routing overview', async () => {
    http.get.mockResolvedValue({ data: { summary: { eligible_routes: 2 } } })
    await expect(apiKeyApi.getRoutingOverview({ include_details: false })).resolves.toMatchObject({ summary: { eligible_routes: 2 } })
    expect(http.get).toHaveBeenCalledWith('/platform/api-keys/routing-overview', { params: { include_details: false } })
  })

  it('loads and updates business role provider order', async () => {
    http.get.mockResolvedValue({ data: { items: [{ role: 'ads_review' }] } })
    http.patch.mockResolvedValue({ data: { role: 'ads_review' } })
    await expect(apiKeyApi.listRoleGroups()).resolves.toEqual([{ role: 'ads_review' }])
    await apiKeyApi.updateRoleProviderOrder('ads_review', ['sub2api', 'toapis'])
    expect(http.get).toHaveBeenCalledWith('/platform/api-keys/role-groups')
    expect(http.patch).toHaveBeenCalledWith(
      '/platform/api-keys/role-groups/ads_review/provider-order',
      { provider_order: ['sub2api', 'toapis'] },
    )
  })

  it('loads paged model and route catalogs', async () => {
    http.get.mockResolvedValue({ data: { items: [], page: 2, page_size: 50, total: 75 } })
    await apiKeyApi.listCatalogModels({ page: 2, page_size: 50, capability: 'text' })
    await apiKeyApi.listRoutes({ page: 2, page_size: 50, search: 'coultra' })
    expect(http.get).toHaveBeenNthCalledWith(1, '/platform/api-keys/catalog-models', { params: { page: 2, page_size: 50, capability: 'text' } })
    expect(http.get).toHaveBeenNthCalledWith(2, '/platform/api-keys/routes', { params: { page: 2, page_size: 50, search: 'coultra' } })
  })

  it('uses explicit endpoints for discovery and route control', async () => {
    http.post.mockResolvedValue({ data: { ok: true } })
    http.patch.mockResolvedValue({ data: { id: 9, priority: 20 } })
    await apiKeyApi.discoverKey(7)
    await apiKeyApi.probeRoute(9, { enable_on_success: true })
    await apiKeyApi.resetRouteCircuit(9)
    await apiKeyApi.updateRoute(9, { priority: 20 })
    expect(http.post).toHaveBeenNthCalledWith(1, '/platform/api-keys/keys/7/discover')
    expect(http.post).toHaveBeenNthCalledWith(2, '/platform/api-keys/routes/9/probe', { enable_on_success: true })
    expect(http.post).toHaveBeenNthCalledWith(3, '/platform/api-keys/routes/9/reset-circuit')
    expect(http.patch).toHaveBeenCalledWith('/platform/api-keys/routes/9', { priority: 20 })
  })
})
