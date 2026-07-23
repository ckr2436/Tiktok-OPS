import http from '@/core/httpClient.js'


function prefix(workspaceId) {
  return `/tenants/${encodeURIComponent(workspaceId)}/oauth/tiktok-shop`
}


function dataPrefix(workspaceId) {
  return `/tenants/${encodeURIComponent(workspaceId)}/tiktok-shop`
}


function payload(response) {
  return response?.data ?? response
}


function paginationError(message, details = {}) {
  const error = new Error(message)
  error.code = 'TIKTOK_SHOP_PAGINATION_INVARIANT'
  Object.assign(error, details)
  return error
}


export async function getTikTokShopReadiness(workspaceId) {
  const response = await http.get(`${prefix(workspaceId)}/readiness`)
  return response?.data ?? response
}


export async function createTikTokShopAuthorization(workspaceId, payload = {}) {
  const response = await http.post(`${prefix(workspaceId)}/authz`, payload)
  return response?.data ?? response
}


export async function listTikTokShopAccounts(workspaceId) {
  const response = await http.get(`${prefix(workspaceId)}/accounts`)
  const data = response?.data ?? response
  return Array.isArray(data?.items) ? data.items : []
}


export async function syncTikTokShopAccount(workspaceId, accountId) {
  const response = await http.post(
    `${prefix(workspaceId)}/accounts/${encodeURIComponent(accountId)}/sync`,
    {},
  )
  return response?.data ?? response
}


export async function refreshTikTokShopAccount(workspaceId, accountId) {
  const response = await http.post(
    `${prefix(workspaceId)}/accounts/${encodeURIComponent(accountId)}/refresh`,
    {},
  )
  return response?.data ?? response
}


export async function disconnectTikTokShopAccount(workspaceId, accountId) {
  const response = await http.post(
    `${prefix(workspaceId)}/accounts/${encodeURIComponent(accountId)}/disconnect`,
    {},
  )
  return response?.data ?? response
}


export async function deleteTikTokShopAccount(workspaceId, accountId) {
  const response = await http.delete(
    `${prefix(workspaceId)}/accounts/${encodeURIComponent(accountId)}`,
  )
  return response?.data ?? response
}


export async function getTikTokShopAnalytics(
  workspaceId,
  dataset,
  params = {},
  config = {},
) {
  return payload(await http.get(
    `${dataPrefix(workspaceId)}/analytics/${encodeURIComponent(dataset)}`,
    { ...config, params },
  ))
}


export async function getTikTokShopGuardFeed(
  workspaceId,
  shopId,
  params = {},
  config = {},
) {
  return payload(await http.get(
    `${dataPrefix(workspaceId)}/operations/guard-feed`,
    { ...config, params: { shop_id: shopId, ...params } },
  ))
}


export async function getAllTikTokShopAnalytics(
  workspaceId,
  dataset,
  params = {},
  config = {},
) {
  const pageSize = 500
  const items = []
  const seenIds = new Set()
  let page = 1
  let response = null
  let expectedTotal = null

  // The API caps a reporting range at 366 days. A hard page ceiling keeps a
  // malformed provider total from turning a screen refresh into an endless loop.
  while (page <= 100) {
    response = await getTikTokShopAnalytics(
      workspaceId,
      dataset,
      { ...params, page, page_size: pageSize },
      config,
    )
    const rows = Array.isArray(response?.items) ? response.items : []
    const total = Number(response?.total || 0)
    if (Number(response?.page) !== page) {
      throw paginationError('短视频数据分页异常：后端返回的页码与请求不一致，已停止展示以避免漏数。', { page })
    }
    if (!Number.isInteger(total) || total < 0) {
      throw paginationError('短视频数据分页异常：后端返回了无效的数据总数。', { page })
    }
    if (expectedTotal === null) expectedTotal = total
    if (total !== expectedTotal) {
      throw paginationError('短视频数据正在变化，请稍后刷新后重新查看。', { page })
    }
    if (rows.length === 0 && items.length < total) {
      throw paginationError('短视频数据分页不完整：尚未拉满总数就出现空页，已停止展示以避免漏数。', { page })
    }
    for (const row of rows) {
      const id = String(row?.id ?? '')
      if (!id) {
        throw paginationError('短视频数据缺少唯一标识，无法安全合并分页结果。', { page })
      }
      if (seenIds.has(id)) {
        throw paginationError('短视频数据分页异常：不同页面返回了重复记录，已停止展示以避免漏数或重复计算。', { page, duplicate_id: id })
      }
      seenIds.add(id)
      items.push(row)
    }
    if (items.length >= total) break
    if (rows.length < pageSize) {
      throw paginationError('短视频数据分页提前结束，已停止展示以避免使用不完整报表。', { page })
    }
    page += 1
  }

  if (items.length !== Number(expectedTotal || 0)) {
    throw paginationError('短视频数据量超过安全分页上限，未能完整加载。', { page })
  }

  return {
    ...(response || {}),
    items,
    page: 1,
    page_size: items.length,
    total: Number(expectedTotal ?? items.length),
    pages_loaded: page,
  }
}


export async function lookupTikTokShopVideoMedia(
  workspaceId,
  shopId,
  videoIds = [],
  config = {},
) {
  const normalized = [...new Set(
    (Array.isArray(videoIds) ? videoIds : [])
      .map((value) => String(value || '').trim())
      .filter(Boolean),
  )]
  const items = []
  const matchedIds = new Set()
  const statusCounts = {}
  let cached = 0
  const chunkSize = 200

  for (let offset = 0; offset < normalized.length; offset += chunkSize) {
    const chunk = normalized.slice(offset, offset + chunkSize)
    const response = payload(await http.post(
      `${dataPrefix(workspaceId)}/video-media/lookup`,
      { shop_id: Number(shopId), video_ids: chunk },
      config,
    ))
    cached += Number(response?.matched || 0)
    for (const [status, count] of Object.entries(response?.status_counts || {})) {
      statusCounts[status] = Number(statusCounts[status] || 0) + Number(count || 0)
    }
    for (const item of Array.isArray(response?.items) ? response.items : []) {
      const videoId = String(item?.video_id || '')
      if (!videoId || matchedIds.has(videoId)) continue
      matchedIds.add(videoId)
      items.push(item)
    }
  }

  return {
    items,
    requested: normalized.length,
    matched: cached,
    status_counts: statusCounts,
  }
}


export async function lookupTikTokShopVideoAnalyses(
  workspaceId,
  shopId,
  videoIds = [],
  range = {},
  config = {},
) {
  const normalized = [...new Set(
    (Array.isArray(videoIds) ? videoIds : [])
      .map((value) => String(value || '').trim())
      .filter(Boolean),
  )]
  const items = []
  const chunkSize = 200
  for (let offset = 0; offset < normalized.length; offset += chunkSize) {
    const response = payload(await http.post(
      `${dataPrefix(workspaceId)}/video-content-analyses/lookup`,
      {
        shop_id: Number(shopId),
        video_ids: normalized.slice(offset, offset + chunkSize),
        start_date: range.start_date,
        end_date_exclusive: range.end_date_exclusive,
      },
      config,
    ))
    items.push(...(Array.isArray(response?.items) ? response.items : []))
  }
  return { items, requested: normalized.length, matched: items.length }
}


export async function requestTikTokShopVideoAnalysis(
  workspaceId,
  payloadValue,
  config = {},
) {
  return payload(await http.post(
    `${dataPrefix(workspaceId)}/video-content-analyses`,
    payloadValue,
    config,
  ))
}


export async function getAllTikTokShopProducts(
  workspaceId,
  shopId,
  config = {},
) {
  const pageSize = 200
  const items = []
  const seenProductIds = new Set()
  let page = 1
  let expectedTotal = null

  while (page <= 100) {
    const response = payload(await http.get(
      `${dataPrefix(workspaceId)}/products`,
      { ...config, params: { shop_id: shopId, page, page_size: pageSize } },
    ))
    const rows = Array.isArray(response?.items) ? response.items : []
    const total = Number(response?.total || 0)
    if (!Number.isInteger(total) || total < 0) {
      throw paginationError('商品目录返回了无效的数据总数，无法安全加载商品封面。', { page })
    }
    if (expectedTotal === null) expectedTotal = total
    if (total !== expectedTotal) {
      throw paginationError('商品目录正在变化，请稍后刷新后重新查看。', { page })
    }
    for (const row of rows) {
      const productId = String(row?.product_id || '').trim()
      if (!productId || seenProductIds.has(productId)) continue
      seenProductIds.add(productId)
      items.push(row)
    }
    if (items.length >= total || rows.length === 0) break
    if (rows.length < pageSize) {
      throw paginationError('商品目录分页提前结束，无法安全加载完整商品封面。', { page })
    }
    page += 1
  }

  if (items.length !== Number(expectedTotal || 0)) {
    throw paginationError('商品目录未能完整加载。', { page })
  }
  return { items, total: items.length, pages_loaded: page }
}
