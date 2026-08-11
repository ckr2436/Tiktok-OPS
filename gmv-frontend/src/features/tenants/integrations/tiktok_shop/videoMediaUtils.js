export function buildVideoMediaMap(items = []) {
  return new Map(
    (Array.isArray(items) ? items : [])
      .filter((item) => item?.video_id)
      .map((item) => [String(item.video_id), item]),
  )
}

export function buildProductMap(items = []) {
  return new Map(
    (Array.isArray(items) ? items : [])
      .filter((item) => item?.product_id)
      .map((item) => [String(item.product_id), item]),
  )
}

export function videoMediaState(status) {
  switch (String(status || '').toUpperCase()) {
    case 'READY':
      return { label: '本地视频', tone: 'ready' }
    case 'PARTIAL':
      return { label: '部分媒体可用', tone: 'warning' }
    case 'SOURCE_EXPIRED':
      return { label: '素材地址已过期', tone: 'warning' }
    case 'NOT_IN_GMVMAX_LIBRARY':
      return { label: 'GMV Max 素材不可用', tone: 'muted' }
    case 'INVALID_VIDEO_ID':
      return { label: '无效视频 ID', tone: 'danger' }
    case 'PENDING':
    case 'QUEUED':
    case 'PROCESSING':
      return { label: '媒体缓存中', tone: 'loading' }
    case 'ERROR':
    case 'MEDIA_UNAVAILABLE':
      return { label: '媒体缓存失败', tone: 'danger' }
    default:
      return { label: '媒体状态未知', tone: 'muted' }
  }
}

export function resolveVideoPresentation(video, media, productMap) {
  const products = Array.isArray(video?.products) ? video.products : []
  const fallbackProduct = products
    .map((product) => productMap.get(String(product?.id || '')))
    .find((product) => product?.main_image_url)
  const videoCover = String(media?.cover_url || '').trim()
  const productCover = String(fallbackProduct?.main_image_url || '').trim()
  const previewUrl = String(media?.preview_url || '').trim()
  const status = videoMediaState(media?.media_status)

  return {
    preview_url: previewUrl || null,
    poster_url: videoCover || productCover || null,
    poster_kind: videoCover ? 'VIDEO_COVER' : productCover ? 'PRODUCT_IMAGE' : 'NONE',
    fallback_product_id: fallbackProduct?.product_id || null,
    status_label: status.label,
    status_tone: status.tone,
  }
}
