import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/core/httpClient.js', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import http from '@/core/httpClient.js'
import {
  createTikTokShopContentPost,
  createTikTokShopCreatorAuthorization,
  getTikTokShopCreatorProducts,
  publishTikTokShopContentPost,
} from './service.js'


describe('TikTok Shop content posting service', () => {
  beforeEach(() => {
    http.get.mockReset()
    http.post.mockReset()
  })

  it('starts the official creator authorization flow explicitly', async () => {
    http.post.mockResolvedValue({ data: { auth_url: 'https://shop.tiktok.com/creator-auth' } })

    await createTikTokShopCreatorAuthorization(3, { return_to: '/return' })

    expect(http.post).toHaveBeenCalledWith(
      '/tenants/3/oauth/tiktok-shop/authz',
      { return_to: '/return', authorization_type: 'creator' },
    )
  })

  it('loads creator products without a seller shop cipher', async () => {
    http.get.mockResolvedValue({ data: { data: { products: [] }, request_id: 'request-1' } })

    await getTikTokShopCreatorProducts(3, 9, { page_size: 100 })

    expect(http.get).toHaveBeenCalledWith(
      '/tenants/3/tiktok-shop/content-posting/accounts/9/shop-products',
      { params: { page_size: 100 } },
    )
  })

  it('keeps the idempotency key on the multipart workflow request', async () => {
    http.post.mockResolvedValue({ data: { item: { id: 12 }, reused: false } })
    const video = new File(['video'], 'video.mp4', { type: 'video/mp4' })

    await createTikTokShopContentPost(3, {
      accountId: 9,
      productId: 'product-1',
      productLinkTitle: 'Sleep Gummies',
      video,
      videoTitle: 'Caption',
      coverTimestampMs: 1000,
      idempotencyKey: 'tt-post-key-123',
    })

    const [url, form, config] = http.post.mock.calls[0]
    expect(url).toBe('/tenants/3/tiktok-shop/content-posting/posts')
    expect(form).toBeInstanceOf(FormData)
    expect(form.get('account_id')).toBe('9')
    expect(form.get('product_id')).toBe('product-1')
    expect(form.get('video')).toBe(video)
    expect(config.headers['Idempotency-Key']).toBe('tt-post-key-123')
  })

  it('uses a separate explicit call for formal publication', async () => {
    http.post.mockResolvedValue({ data: { id: 12, publish_requested: true } })

    await publishTikTokShopContentPost(3, 12)

    expect(http.post).toHaveBeenCalledWith(
      '/tenants/3/tiktok-shop/content-posting/posts/12/publish',
      {},
      {},
    )
  })
})
