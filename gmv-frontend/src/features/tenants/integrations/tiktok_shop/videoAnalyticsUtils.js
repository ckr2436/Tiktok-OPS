function number(value) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function nullableNumber(value) {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function latestRow(left, right) {
  const leftDate = String(left?.report_date || '')
  const rightDate = String(right?.report_date || '')
  if (rightDate !== leftDate) return rightDate > leftDate ? right : left
  return String(right?.synced_at || '') > String(left?.synced_at || '') ? right : left
}

export function nextDate(value) {
  if (!value) return ''
  const date = new Date(`${value}T12:00:00Z`)
  if (Number.isNaN(date.getTime())) return ''
  date.setUTCDate(date.getUTCDate() + 1)
  return date.toISOString().slice(0, 10)
}

export function formatVideoPublishedAt(value, timezone = 'UTC') {
  if (!value) return '--'
  const raw = String(value)
  const parsed = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(raw) ? raw : `${raw}Z`)
  if (Number.isNaN(parsed.getTime())) return raw
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: timezone || 'UTC',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(parsed)
  } catch {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'UTC',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(parsed)
  }
}

export function aggregateVideoRows(rows = []) {
  const videos = new Map()

  for (const row of Array.isArray(rows) ? rows : []) {
    const id = String(row?.video_id || '').trim()
    if (!id) continue
    const current = videos.get(id)
    const newest = current ? latestRow(current.latest, row) : row
    const views = number(row.views)
    const ctr = number(row.click_through_rate)
    const products = Array.isArray(row.products_json) ? row.products_json : []
    const previousProducts = current?.products || []
    const productMap = new Map(
      [...previousProducts, ...products]
        .filter((item) => item && (item.id || item.name))
        .map((item) => [String(item.id || item.name), item]),
    )

    videos.set(id, {
      video_id: id,
      latest: newest,
      title: newest.title || current?.title || `视频 ${id}`,
      creator_username: newest.creator_username || current?.creator_username || '',
      author_type: newest.author_type || current?.author_type || '',
      video_post_time: newest.video_post_time || current?.video_post_time || null,
      duration_seconds: newest.duration_seconds ?? current?.duration_seconds ?? null,
      currency: newest.currency || current?.currency || 'USD',
      report_date: newest.report_date,
      synced_at: String(newest.synced_at || current?.synced_at || ''),
      gmv: number(current?.gmv) + number(row.gmv),
      views: number(current?.views) + views,
      sku_orders: number(current?.sku_orders) + number(row.sku_orders),
      items_sold: number(current?.items_sold) + number(row.items_sold),
      ctr_weight: number(current?.ctr_weight) + Math.max(views, 1),
      ctr_weighted_sum: number(current?.ctr_weighted_sum) + ctr * Math.max(views, 1),
      report_days: number(current?.report_days) + 1,
      products: [...productMap.values()],
    })
  }

  return [...videos.values()].map((video) => {
    const clickThroughRate = video.ctr_weight > 0
      ? video.ctr_weighted_sum / video.ctr_weight
      : 0
    return {
      ...video,
      gpm: video.views > 0 ? (video.gmv / video.views) * 1000 : 0,
      click_through_rate: clickThroughRate,
      // The Shop per-video API exposes views and CTR, but not click count.
      // Keep the calculated value explicitly estimated in the UI.
      estimated_product_clicks: Math.round(video.views * clickThroughRate),
    }
  })
}

export function buildDailyVideoSeries(videoRows = [], overviewRows = []) {
  const days = new Map()
  for (const row of Array.isArray(overviewRows) ? overviewRows : []) {
    const date = String(row?.report_date || '')
    if (!date) continue
    days.set(date, {
      date,
      gmv: number(row.gmv),
      views: 0,
      orders: number(row.sku_orders),
      impressions: number(row.product_impressions),
      clicks: number(row.product_clicks),
      videos: new Set(),
    })
  }
  for (const row of Array.isArray(videoRows) ? videoRows : []) {
    const date = String(row?.report_date || '')
    if (!date) continue
    const day = days.get(date) || {
      date,
      gmv: 0,
      views: 0,
      orders: 0,
      impressions: 0,
      clicks: 0,
      videos: new Set(),
    }
    if (!days.has(date)) {
      day.gmv += number(row.gmv)
      day.orders += number(row.sku_orders)
    }
    day.views += number(row.views)
    if (row.video_id) day.videos.add(String(row.video_id))
    days.set(date, day)
  }
  return [...days.values()]
    .sort((left, right) => left.date.localeCompare(right.date))
    .map((day) => ({ ...day, videos: day.videos.size }))
}

export function summarizeVideoAnalytics(videoRows = [], overviewRows = []) {
  const videos = aggregateVideoRows(videoRows)
  const overview = Array.isArray(overviewRows) ? overviewRows : []
  const gmv = overview.reduce((total, row) => total + number(row.gmv), 0)
  const views = videos.reduce((total, row) => total + number(row.views), 0)
  const orders = overview.reduce((total, row) => total + number(row.sku_orders), 0)
  const impressions = overview.reduce(
    (total, row) => total + number(row.product_impressions),
    0,
  )
  const clicks = overview.reduce((total, row) => total + number(row.product_clicks), 0)
  const overviewDates = new Set(
    overview.map((row) => String(row?.report_date || '')).filter(Boolean),
  )
  const videoDates = new Set(
    videoRows.map((row) => String(row?.report_date || '')).filter(Boolean),
  )
  const detailCoversOverview = overviewDates.size > 0
    && [...overviewDates].every((date) => videoDates.has(date))
  const singleOfficialCtr = overview.length === 1
    ? nullableNumber(overview[0]?.click_through_rate)
    : null
  // TikTok defines video overview click_through_rate as product clicks divided
  // by video views. Product impressions are a separate official field and must
  // never be substituted as the denominator.
  const ctr = singleOfficialCtr ?? (
    detailCoversOverview && views > 0 ? clicks / views : null
  )
  const syncedAt = [...videoRows, ...overview]
    .map((row) => String(row?.synced_at || ''))
    .sort()
    .at(-1) || null
  const latestReportDate = [...videoRows, ...overview]
    .map((row) => String(row?.report_date || ''))
    .filter(Boolean)
    .sort()
    .at(-1) || null
  const latestVideoDate = videoRows
    .map((row) => String(row?.report_date || ''))
    .filter(Boolean)
    .sort()
    .at(-1) || null

  return {
    videos,
    gmv,
    views,
    orders,
    impressions,
    clicks,
    ctr,
    product_impression_click_rate: impressions > 0 ? clicks / impressions : null,
    gpm: views > 0 ? (gmv / views) * 1000 : 0,
    latest_synced_at: syncedAt,
    latest_report_date: latestReportDate,
    latest_video_date: latestVideoDate,
  }
}

export function summarizeOverviewDay(rows = [], reportDate = '') {
  const matching = (Array.isArray(rows) ? rows : [])
    .filter((row) => String(row?.report_date || '') === String(reportDate || ''))
  if (!matching.length) {
    return {
      report_date: reportDate || null,
      available: false,
      gmv: null,
      sku_orders: null,
      product_impressions: null,
      product_clicks: null,
      click_through_rate: null,
      data_source: null,
      is_provisional: false,
      synced_at: null,
    }
  }
  const newest = matching.reduce(latestRow)
  return {
    report_date: reportDate,
    available: true,
    currency: newest.currency || 'USD',
    gmv: matching.reduce((total, row) => total + number(row.gmv), 0),
    sku_orders: matching.reduce((total, row) => total + number(row.sku_orders), 0),
    product_impressions: matching.reduce(
      (total, row) => total + number(row.product_impressions), 0,
    ),
    product_clicks: matching.reduce(
      (total, row) => total + number(row.product_clicks), 0,
    ),
    click_through_rate: matching.length === 1
      ? nullableNumber(newest.click_through_rate)
      : null,
    data_source: newest.data_source || 'shop_video_overview',
    is_provisional: Boolean(newest.is_provisional),
    latest_available_date: newest.latest_available_date || null,
    provider_request_id: newest.provider_request_id || null,
    synced_at: newest.synced_at || null,
  }
}

export function filterAndSortVideos(videos = [], search = '', sortKey = 'gmv') {
  const needle = String(search || '').trim().toLowerCase()
  const filtered = (Array.isArray(videos) ? videos : []).filter((video) => {
    if (!needle) return true
    const searchable = [
      video.video_id,
      video.title,
      video.creator_username,
      ...(video.products || []).flatMap((product) => [product.id, product.name]),
    ].join(' ').toLowerCase()
    return searchable.includes(needle)
  })
  return [...filtered].sort((left, right) => {
    const leftValue = sortKey === 'diagnosis_priority'
      ? left?.diagnosis?.priority
      : left?.[sortKey]
    const rightValue = sortKey === 'diagnosis_priority'
      ? right?.diagnosis?.priority
      : right?.[sortKey]
    const difference = number(rightValue) - number(leftValue)
    if (difference !== 0) return difference
    return number(right.gmv) - number(left.gmv)
  })
}

function median(values = []) {
  const sorted = values.map(number).filter((value) => value > 0).sort((a, b) => a - b)
  if (!sorted.length) return 0
  const middle = Math.floor(sorted.length / 2)
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2
}

function sumRows(rows = []) {
  return rows.reduce((total, row) => ({
    gmv: total.gmv + number(row.gmv),
    views: total.views + number(row.views),
    orders: total.orders + number(row.sku_orders),
  }), { gmv: 0, views: 0, orders: 0 })
}

function diagnosis(code, overrides = {}) {
  const catalog = {
    WINNER: {
      label: '爆款放大型', tone: 'success', priority: 90,
      summary: '流量和变现效率均高于当前店铺基准。',
      recommendation: '保持投放稳定，并复刻开头、卖点与转化结构。',
    },
    POTENTIAL: {
      label: '潜力投放型', tone: 'info', priority: 80,
      summary: '当前流量不高，但单位播放产出表现突出。',
      recommendation: '进入小预算测试，观察放量后转化是否稳定。',
    },
    TRAFFIC_NO_CLICK: {
      label: '引流不转化', tone: 'warning', priority: 75,
      summary: '播放量较高，但商品点击率明显低于店铺基准。',
      recommendation: '加强商品露出、利益点和购买行动引导。',
    },
    PRODUCT_HANDOFF: {
      label: '商品承接偏弱', tone: 'warning', priority: 70,
      summary: '点击意愿不弱，但订单效率低于当前店铺基准。',
      recommendation: '检查商品价格、详情页、评价和库存承接。',
    },
    DECAYING: {
      label: '素材衰退', tone: 'danger', priority: 95,
      summary: '后半周期GMV较前半周期明显下降。',
      recommendation: '控制继续放量，准备新素材替换并保留复盘样本。',
    },
    STABLE: {
      label: '稳定观察', tone: 'neutral', priority: 30,
      summary: '当前表现接近店铺基准，暂未出现明确机会或风险。',
      recommendation: '保持观察，等待更多流量或转化样本。',
    },
    INSUFFICIENT_DATA: {
      label: '数据不足', tone: 'muted', priority: 10,
      summary: '当前样本不足，过早判断可能误导投放。',
      recommendation: '继续积累样本，不自动执行预算或暂停动作。',
    },
  }
  return { code, ...catalog[code], ...overrides, rule_version: 'shop-video-rules-v1' }
}

export function diagnoseVideos(videoRows = []) {
  const videos = aggregateVideoRows(videoRows)
  const viewsMedian = median(videos.map((item) => item.views))
  const gpmMedian = median(videos.map((item) => item.gpm))
  const ctrMedian = median(videos.map((item) => item.click_through_rate))
  const orderRateMedian = median(videos.map((item) => (
    item.views > 0 ? item.sku_orders / item.views : 0
  )))
  const rowsByVideo = new Map()
  for (const row of Array.isArray(videoRows) ? videoRows : []) {
    const id = String(row?.video_id || '')
    if (!id) continue
    const rows = rowsByVideo.get(id) || []
    rows.push(row)
    rowsByVideo.set(id, rows)
  }

  return videos.map((video) => {
    const dailyRows = (rowsByVideo.get(video.video_id) || [])
      .slice()
      .sort((left, right) => String(left.report_date).localeCompare(String(right.report_date)))
    const split = Math.max(1, Math.floor(dailyRows.length / 2))
    const previous = sumRows(dailyRows.slice(0, split))
    const current = sumRows(dailyRows.slice(split))
    const trend = previous.gmv > 0 ? (current.gmv - previous.gmv) / previous.gmv : null
    const orderRate = video.views > 0 ? video.sku_orders / video.views : 0
    let result

    if (dailyRows.length < 2 || video.views < 100) {
      result = diagnosis('INSUFFICIENT_DATA')
    } else if (previous.gmv >= 1 && current.gmv < previous.gmv * 0.6) {
      result = diagnosis('DECAYING')
    } else if (video.views >= viewsMedian && video.gpm >= gpmMedian && video.sku_orders > 0) {
      result = diagnosis('WINNER')
    } else if (video.views < viewsMedian && video.gpm >= gpmMedian && video.sku_orders > 0) {
      result = diagnosis('POTENTIAL')
    } else if (
      video.views >= viewsMedian
      && ctrMedian > 0
      && video.click_through_rate < ctrMedian * 0.7
    ) {
      result = diagnosis('TRAFFIC_NO_CLICK')
    } else if (
      video.click_through_rate >= ctrMedian
      && orderRateMedian > 0
      && orderRate < orderRateMedian * 0.7
    ) {
      result = diagnosis('PRODUCT_HANDOFF')
    } else {
      result = diagnosis('STABLE')
    }

    return {
      ...video,
      diagnosis: {
        ...result,
        evidence: {
          report_days: dailyRows.length,
          views: video.views,
          views_median: viewsMedian,
          gpm: video.gpm,
          gpm_median: gpmMedian,
          click_through_rate: video.click_through_rate,
          ctr_median: ctrMedian,
          order_rate: orderRate,
          order_rate_median: orderRateMedian,
          period_change: trend,
        },
      },
    }
  })
}

export function summarizeDiagnoses(videos = []) {
  const counts = {}
  for (const video of Array.isArray(videos) ? videos : []) {
    const code = video?.diagnosis?.code || 'INSUFFICIENT_DATA'
    counts[code] = number(counts[code]) + 1
  }
  const actionable = (Array.isArray(videos) ? videos : [])
    .filter((video) => !['STABLE', 'INSUFFICIENT_DATA'].includes(video?.diagnosis?.code))
    .slice()
    .sort((left, right) => (
      number(right?.diagnosis?.priority) - number(left?.diagnosis?.priority)
      || number(right.gmv) - number(left.gmv)
    ))
  return { counts, actionable }
}

export function buildProductMatrix(productRows = [], catalogItems = [], videos = []) {
  const catalog = new Map(
    (Array.isArray(catalogItems) ? catalogItems : [])
      .filter((item) => item?.product_id)
      .map((item) => [String(item.product_id), item]),
  )
  const metrics = new Map()
  for (const row of Array.isArray(productRows) ? productRows : []) {
    const id = String(row?.product_id || '')
    if (!id) continue
    const current = metrics.get(id) || {
      product_id: id, gmv: 0, orders: 0, impressions: 0, clicks: 0,
      refunds: 0, currency: row.currency || 'USD', synced_at: null,
    }
    current.gmv += number(row.gmv)
    current.orders += number(row.orders || row.sku_orders)
    current.impressions += number(row.product_impressions)
    current.clicks += number(row.product_clicks)
    current.refunds += number(row.refund_amount)
    current.synced_at = String(row.synced_at || '') > String(current.synced_at || '')
      ? row.synced_at
      : current.synced_at
    metrics.set(id, current)
  }
  const linked = new Map()
  for (const video of Array.isArray(videos) ? videos : []) {
    for (const product of Array.isArray(video?.products) ? video.products : []) {
      const id = String(product?.id || '')
      if (!id) continue
      const bucket = linked.get(id) || []
      bucket.push(video)
      linked.set(id, bucket)
    }
  }
  const ids = new Set([...metrics.keys(), ...linked.keys()])
  return [...ids].map((id) => {
    const metric = metrics.get(id) || {
      product_id: id, gmv: 0, orders: 0, impressions: 0, clicks: 0,
      refunds: 0, currency: 'USD', synced_at: null,
    }
    const product = catalog.get(id) || {}
    const related = linked.get(id) || []
    const active = related.filter((video) => number(video.sku_orders) > 0)
    const opportunities = related.filter((video) => (
      ['WINNER', 'POTENTIAL'].includes(video?.diagnosis?.code)
    ))
    let contentStatus = '内容稳定'
    let contentTone = 'neutral'
    if (!related.length) {
      contentStatus = '缺少视频关联'
      contentTone = 'danger'
    } else if (!active.length) {
      contentStatus = '有内容但未成交'
      contentTone = 'warning'
    } else if (opportunities.length) {
      contentStatus = '存在放量机会'
      contentTone = 'success'
    } else if (related.every((video) => video?.diagnosis?.code === 'DECAYING')) {
      contentStatus = '素材集中衰退'
      contentTone = 'danger'
    }
    return {
      ...metric,
      title: product.title || product.product_name || related[0]?.products?.find(
        (item) => String(item?.id || '') === id,
      )?.name || `商品 ${id}`,
      image_url: product.main_image_url || null,
      linked_video_count: related.length,
      effective_video_count: active.length,
      opportunity_count: opportunities.length,
      click_through_rate: metric.impressions > 0 ? metric.clicks / metric.impressions : 0,
      refund_rate: metric.gmv > 0 ? metric.refunds / metric.gmv : 0,
      content_status: contentStatus,
      content_tone: contentTone,
    }
  }).sort((left, right) => number(right.gmv) - number(left.gmv))
}

export function dataTrustMeta({ endDate, latestReportDate, today, syncedAt, rowCount, total }) {
  const complete = number(rowCount) === number(total)
  const includesMutableDay = Boolean(
    endDate && latestReportDate && today
    && endDate === today
    && latestReportDate === today,
  )
  return {
    source: 'TikTok Shop 官方分析 API',
    grain: '视频 × 店铺自然日',
    synced_at: syncedAt || null,
    complete,
    completeness_label: complete ? '完整分页' : '数据未拉满',
    freshness_label: includesMutableDay ? '当日可变数据' : '历史固化数据',
    refresh_label: includesMutableDay ? '后台每 5 分钟同步' : '历史日期不会重复改写',
  }
}
