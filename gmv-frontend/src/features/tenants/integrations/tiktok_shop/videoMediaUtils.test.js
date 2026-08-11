import { describe, expect, it } from 'vitest'

import {
  buildProductMap,
  buildVideoMediaMap,
  resolveVideoPresentation,
  videoMediaState,
} from './videoMediaUtils.js'

describe('TikTok Shop video media presentation', () => {
  it('prefers the local video cover and enables local playback', () => {
    const media = buildVideoMediaMap([{
      video_id: 'video-1',
      media_status: 'READY',
      cover_url: '/local/cover.jpg',
      preview_url: '/local/video.mp4',
    }]).get('video-1')
    const productMap = buildProductMap([{
      product_id: 'product-1',
      main_image_url: '/product.jpg',
    }])

    expect(resolveVideoPresentation(
      { products: [{ id: 'product-1' }] },
      media,
      productMap,
    )).toMatchObject({
      poster_url: '/local/cover.jpg',
      poster_kind: 'VIDEO_COVER',
      preview_url: '/local/video.mp4',
      status_label: '本地视频',
    })
  })

  it('falls back to the linked product image without pretending it is a video cover', () => {
    const productMap = buildProductMap([{
      product_id: 'product-1',
      main_image_url: '/product.jpg',
    }])

    expect(resolveVideoPresentation(
      { products: [{ id: 'product-1' }] },
      { media_status: 'NOT_IN_GMVMAX_LIBRARY' },
      productMap,
    )).toMatchObject({
      poster_url: '/product.jpg',
      poster_kind: 'PRODUCT_IMAGE',
      preview_url: null,
      status_label: 'GMV Max 素材不可用',
    })
  })

  it('labels expired and invalid provider media distinctly', () => {
    expect(videoMediaState('SOURCE_EXPIRED')).toMatchObject({ tone: 'warning' })
    expect(videoMediaState('INVALID_VIDEO_ID')).toMatchObject({ tone: 'danger' })
  })
})
