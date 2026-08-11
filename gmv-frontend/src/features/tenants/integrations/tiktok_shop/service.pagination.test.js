import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/core/httpClient.js', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

import http from '@/core/httpClient.js'
import {
  createTikTokShopVideoAnalysisHandoff,
  downloadTikTokShopVideoAnalysisReport,
  getAllTikTokShopAnalytics,
  getAllTikTokShopProducts,
  lookupTikTokShopVideoMedia,
  lookupTikTokShopVideoAnalyses,
  requestTikTokShopVideoAnalysis,
} from './service.js'

describe('TikTok Shop analytics pagination', () => {
  beforeEach(() => {
    http.get.mockReset()
    http.post.mockReset()
  })

  it('loads every page before returning video analytics', async () => {
    const rows = Array.from({ length: 1001 }, (_, index) => ({
      id: index + 1,
      video_id: `video-${index + 1}`,
    }))
    http.get.mockImplementation((url, config) => {
      if (!String(url).includes('/analytics/videos')) return Promise.resolve({ data: {} })
      const page = Number(config.params.page)
      const pageSize = Number(config.params.page_size)
      const start = (page - 1) * pageSize
      return Promise.resolve({
        data: {
          items: rows.slice(start, start + pageSize),
          page,
          page_size: pageSize,
          total: rows.length,
        },
      })
    })

    const result = await getAllTikTokShopAnalytics(3, 'videos', { shop_id: 1 })

    const analyticsCalls = http.get.mock.calls.filter(([url]) => String(url).includes('/analytics/videos'))
    expect(analyticsCalls).toHaveLength(3)
    expect(analyticsCalls.map(([, config]) => config.params.page)).toEqual([1, 2, 3])
    expect(result.items).toEqual(rows)
    expect(result).toMatchObject({ total: 1001, pages_loaded: 3 })
  })

  it('fails closed when the backend stops before its advertised total', async () => {
    http.get.mockResolvedValue({
      data: { items: [], page: 1, page_size: 500, total: 1 },
    })

    await expect(getAllTikTokShopAnalytics(3, 'videos', { shop_id: 1 }))
      .rejects.toMatchObject({ code: 'TIKTOK_SHOP_PAGINATION_INVARIANT', page: 1 })
  })

  it('fails closed when a row is repeated across pages', async () => {
    const firstPage = Array.from({ length: 500 }, (_, index) => ({ id: index + 1 }))
    http.get
      .mockResolvedValueOnce({ data: { items: firstPage, page: 1, page_size: 500, total: 501 } })
      .mockResolvedValueOnce({ data: { items: [{ id: 1 }], page: 2, page_size: 500, total: 501 } })

    await expect(getAllTikTokShopAnalytics(3, 'videos', { shop_id: 1 }))
      .rejects.toMatchObject({
        code: 'TIKTOK_SHOP_PAGINATION_INVARIANT',
        page: 2,
        duplicate_id: '1',
      })
  })

  it('keeps explicit media states while chunking video lookups', async () => {
    const videoIds = Array.from({ length: 201 }, (_, index) => `video-${index + 1}`)
    http.post.mockImplementation((_url, body) => Promise.resolve({
      data: {
        items: body.video_ids.map((videoId) => ({
          video_id: videoId,
          media_status: videoId === 'video-201' ? 'SOURCE_EXPIRED' : 'READY',
          preview_url: videoId === 'video-201' ? null : `/video/${videoId}`,
        })),
        matched: body.video_ids.length - (body.video_ids.includes('video-201') ? 1 : 0),
        status_counts: body.video_ids.includes('video-201')
          ? { READY: body.video_ids.length - 1, SOURCE_EXPIRED: 1 }
          : { READY: body.video_ids.length },
      },
    }))

    const result = await lookupTikTokShopVideoMedia(3, 1, videoIds)

    expect(http.post).toHaveBeenCalledTimes(2)
    expect(result.items).toHaveLength(201)
    expect(result).toMatchObject({
      requested: 201,
      matched: 200,
      status_counts: { READY: 200, SOURCE_EXPIRED: 1 },
    })
  })

  it('loads the complete product catalog for thumbnail fallback', async () => {
    const rows = Array.from({ length: 201 }, (_, index) => ({
      id: index + 1,
      product_id: `product-${index + 1}`,
      main_image_url: `/product-${index + 1}.jpg`,
    }))
    http.get.mockImplementation((_url, config) => {
      const start = (Number(config.params.page) - 1) * Number(config.params.page_size)
      return Promise.resolve({
        data: {
          items: rows.slice(start, start + Number(config.params.page_size)),
          page: config.params.page,
          page_size: config.params.page_size,
          total: rows.length,
        },
      })
    })

    const result = await getAllTikTokShopProducts(3, 1)

    expect(http.get).toHaveBeenCalledTimes(2)
    expect(result.items).toEqual(rows)
    expect(result).toMatchObject({ total: 201, pages_loaded: 2 })
  })

  it('keeps video-analysis lookups bounded and preserves the metric window', async () => {
    const videoIds = Array.from({ length: 201 }, (_, index) => `video-${index + 1}`)
    http.post.mockImplementation((_url, body) => Promise.resolve({
      data: {
        items: body.video_ids.map((videoId) => ({ video_id: videoId, status: 'SUCCEEDED' })),
      },
    }))

    const result = await lookupTikTokShopVideoAnalyses(
      3,
      1,
      videoIds,
      { start_date: '2026-07-01', end_date_exclusive: '2026-07-08' },
    )

    expect(http.post).toHaveBeenCalledTimes(2)
    expect(http.post.mock.calls[0][1]).toMatchObject({
      shop_id: 1,
      start_date: '2026-07-01',
      end_date_exclusive: '2026-07-08',
    })
    expect(http.post.mock.calls[0][1].video_ids).toHaveLength(200)
    expect(result).toMatchObject({ requested: 201, matched: 201 })
  })

  it('submits one explicit, date-scoped analysis request', async () => {
    http.post.mockResolvedValue({ data: { queued: true, item: { id: 9, status: 'QUEUED' } } })
    const body = {
      shop_id: 1,
      video_id: 'video-9',
      start_date: '2026-07-01',
      end_date_exclusive: '2026-07-08',
      retry_failed: false,
    }

    const result = await requestTikTokShopVideoAnalysis(3, body)

    expect(http.post).toHaveBeenCalledWith(
      '/tenants/3/tiktok-shop/video-content-analyses',
      body,
      {},
    )
    expect(result).toMatchObject({ queued: true, item: { status: 'QUEUED' } })
  })

  it('exports the frozen report and creates one explicit content-factory handoff', async () => {
    const blob = new Blob(['report'], { type: 'text/markdown' })
    http.get.mockResolvedValue({
      data: blob,
      headers: { 'content-disposition': 'attachment; filename="report.md"' },
    })
    http.post.mockResolvedValue({
      data: { session_key: 'video-opt-9', content_factory_url: '/tenants/3/hermes-agent/content-factory' },
    })

    const report = await downloadTikTokShopVideoAnalysisReport(3, 9)
    const handoff = await createTikTokShopVideoAnalysisHandoff(3, 9)

    expect(http.get).toHaveBeenCalledWith(
      '/tenants/3/tiktok-shop/video-content-analyses/9/report',
      { params: { format: 'markdown' }, responseType: 'blob' },
    )
    expect(report).toMatchObject({ blob, contentDisposition: 'attachment; filename="report.md"' })
    expect(http.post).toHaveBeenCalledWith(
      '/tenants/3/tiktok-shop/video-content-analyses/9/content-factory-handoff',
      {},
      {},
    )
    expect(handoff.session_key).toBe('video-opt-9')
  })
})
