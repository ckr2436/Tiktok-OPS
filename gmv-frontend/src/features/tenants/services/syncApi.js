import http from '../../../core/httpClient.js'

const DEFAULT_PROVIDER = 'tiktok-business'

function ensureArray(value) {
  if (!value) return []
  return Array.isArray(value) ? value : [value]
}

function toId(value) {
  if (value === undefined || value === null) return ''
  return String(value)
}

function parseNumber(value) {
  if (value === undefined || value === null || value === '') return null
  const num = Number(value)
  return Number.isFinite(num) ? num : null
}

function parseSummary(source) {
  if (!source) {
    return { localCount: 0, remoteCount: null, diff: { added: 0, removed: 0, updated: 0 } }
  }
  const local = parseNumber(
    source.local_count ?? source.localCount ?? source.count_local ?? source.total_local
  )
  const remote = parseNumber(
    source.remote_count ?? source.remoteCount ?? source.count_remote ?? source.total_remote
  )
  const diffSrc = source.diff ?? source.changes ?? source.delta ?? source.difference ?? {}
  const added = parseNumber(diffSrc.added ?? diffSrc.created ?? diffSrc.new) ?? 0
  const removed = parseNumber(diffSrc.removed ?? diffSrc.deleted ?? diffSrc.lost) ?? 0
  const updated = parseNumber(diffSrc.updated ?? diffSrc.modified ?? diffSrc.changed) ?? 0
  return {
    localCount: local ?? 0,
    remoteCount: remote,
    diff: { added, removed, updated },
  }
}

function normalizeLastInfo(raw) {
  if (!raw) return null
  return {
    status: raw.status ?? raw.state ?? raw.result ?? null,
    triggeredAt: raw.triggered_at ?? raw.triggeredAt ?? raw.started_at ?? raw.start_time ?? null,
    finishedAt: raw.finished_at ?? raw.finishedAt ?? raw.completed_at ?? raw.end_time ?? null,
    durationSec: parseNumber(raw.duration_sec ?? raw.duration ?? raw.elapsed_sec),
    nextAllowedAt: raw.next_allowed_at ?? raw.nextAllowedAt ?? null,
    summary: parseSummary(raw.summary ?? raw.last_summary ?? null),
  }
}

function normalizeAdvertiser(raw, ctx, acc) {
  const id = toId(raw.advertiser_id ?? raw.id ?? raw.external_id ?? raw.code)
  if (!id) return null
  const existing = acc.advMap.get(id)
  const base = {
    id,
    bcId: ctx.bcId,
    authId: ctx.authId,
    name: raw.name ?? raw.display_name ?? raw.advertiser_name ?? `Advertiser ${id}`,
    status: (raw.status ?? raw.sync_status ?? raw.state ?? '').toLowerCase() || 'unknown',
    summary: parseSummary(raw.summary ?? raw.last_summary ?? null),
    last: normalizeLastInfo(raw.last_sync ?? raw.last ?? null),
    shopIds: existing?.shopIds ? [...existing.shopIds] : [],
    tags: ensureArray(raw.tags || raw.labels),
  }
  acc.advMap.set(id, base)
  return base
}

function normalizeShop(raw, ctx, acc) {
  const id = toId(raw.shop_id ?? raw.id ?? raw.external_id ?? raw.code)
  if (!id) return null
  const shop = {
    id,
    bcId: ctx.bcId,
    authId: ctx.authId,
    advertiserId: ctx.advertiserId ?? toId(raw.advertiser_id ?? raw.owner_advertiser_id),
    name: raw.name ?? raw.shop_name ?? `Shop ${id}`,
    status: (raw.status ?? raw.sync_status ?? raw.state ?? '').toLowerCase() || 'unknown',
    summary: parseSummary(raw.summary ?? raw.last_summary ?? null),
    last: normalizeLastInfo(raw.last_sync ?? raw.last ?? null),
  }
  acc.shopMap.set(id, shop)
  return shop
}

function normalizeProduct(raw, ctx, acc) {
  const id = toId(raw.product_id ?? raw.id ?? raw.sku_id ?? raw.external_id)
  if (!id) return null
  const product = {
    id,
    title: raw.title ?? raw.name ?? raw.product_name ?? `商品 ${id}`,
    shopId: ctx.shopId ?? toId(raw.shop_id ?? raw.store_id),
    advertiserId: ctx.advertiserId ?? toId(raw.advertiser_id ?? raw.owner_advertiser_id),
    bcId: ctx.bcId,
    authId: ctx.authId,
    status: (raw.status ?? raw.sync_status ?? raw.change_type ?? '').toLowerCase() || 'unknown',
    changeType: (raw.change_type ?? raw.diff_type ?? '').toLowerCase() || null,
    lastChangedAt:
      raw.last_changed_at ?? raw.updated_at ?? raw.modified_at ?? raw.synced_at ?? null,
    summary: parseSummary(raw.summary ?? raw.last_summary ?? null),
    failureReason: raw.error ?? raw.reason ?? raw.failure_reason ?? null,
  }
  acc.productMap.set(id, product)
  return product
}

function normalizeBindingList(items) {
  const acc = {
    bcList: [],
    advMap: new Map(),
    shopMap: new Map(),
    productMap: new Map(),
  }

  for (const item of ensureArray(items)) {
    const authId = toId(item.auth_id ?? item.binding_id ?? item.id)
    const bcRaw = item.bc ?? item.business_center ?? {}
    const bcId = toId(bcRaw.id ?? item.bc_id ?? item.business_center_id ?? authId)
    const bcEntry = {
      id: bcId,
      authId,
      alias: item.alias ?? bcRaw.alias ?? null,
      name: bcRaw.name ?? item.bc_name ?? item.name ?? item.alias ?? `BC ${bcId}`,
      status: (bcRaw.status ?? item.status ?? '').toLowerCase() || 'unknown',
      summary: parseSummary(bcRaw.summary ?? item.summary ?? null),
      last: normalizeLastInfo(item.last_sync ?? bcRaw.last_sync ?? null),
      authExpiresAt: bcRaw.auth_expires_at ?? item.auth_expires_at ?? null,
      advertiserIds: [],
      shopIds: [],
      tags: ensureArray(bcRaw.tags ?? item.tags),
    }

    const advertiserSources = [item.advertisers, bcRaw.advertisers, item.graph?.advertisers]
    for (const advRaw of advertiserSources.flatMap(ensureArray)) {
      const adv = normalizeAdvertiser(advRaw || {}, { bcId, authId }, acc)
      if (!adv) continue
      if (!bcEntry.advertiserIds.includes(adv.id)) bcEntry.advertiserIds.push(adv.id)

      const shopSources = [advRaw?.shops, advRaw?.shop_list]
      for (const shopRaw of shopSources.flatMap(ensureArray)) {
        const shop = normalizeShop(shopRaw || {}, { bcId, authId, advertiserId: adv.id }, acc)
        if (!shop) continue
        if (!adv.shopIds.includes(shop.id)) adv.shopIds.push(shop.id)
        if (!bcEntry.shopIds.includes(shop.id)) bcEntry.shopIds.push(shop.id)

        const productSources = [shopRaw?.products, shopRaw?.items]
        for (const prodRaw of productSources.flatMap(ensureArray)) {
          normalizeProduct(prodRaw || {}, { bcId, authId, advertiserId: adv.id, shopId: shop.id }, acc)
        }
      }
    }

    const shopOnlySources = [item.shops, bcRaw.shops]
    for (const shopRaw of shopOnlySources.flatMap(ensureArray)) {
      const shop = normalizeShop(shopRaw || {}, { bcId, authId }, acc)
      if (!shop) continue
      if (!bcEntry.shopIds.includes(shop.id)) bcEntry.shopIds.push(shop.id)
      const advId = shop.advertiserId
      if (advId) {
        const adv = acc.advMap.get(advId)
        if (adv && !adv.shopIds.includes(shop.id)) adv.shopIds.push(shop.id)
      }
      const productSources = [shopRaw?.products, shopRaw?.items]
      for (const prodRaw of productSources.flatMap(ensureArray)) {
        normalizeProduct(prodRaw || {}, { bcId, authId, advertiserId: shop.advertiserId, shopId: shop.id }, acc)
      }
    }

    const productSources = [item.products, bcRaw.products]
    for (const prodRaw of productSources.flatMap(ensureArray)) {
      normalizeProduct(prodRaw || {}, { bcId, authId }, acc)
    }

    acc.bcList.push(bcEntry)
  }

  const advertisers = Array.from(acc.advMap.values()).map((adv) => ({
    ...adv,
    shopIds: Array.from(new Set(adv.shopIds || [])),
  }))
  const shops = Array.from(acc.shopMap.values())
  const products = Array.from(acc.productMap.values())
  const bcList = acc.bcList.map((bc) => ({
    ...bc,
    advertiserIds: Array.from(new Set(bc.advertiserIds || [])),
    shopIds: Array.from(new Set(bc.shopIds || [])),
  }))

  return { bcList, advertisers, shops, products }
}

function prefix(workspaceId, provider = DEFAULT_PROVIDER) {
  if (!workspaceId) throw new Error('workspaceId is required')
  const wid = encodeURIComponent(workspaceId)
  return `/api/v1/tenants/${wid}/oauth/${provider}`
}

async function listBindings({ workspaceId, provider = DEFAULT_PROVIDER } = {}) {
  const res = await http.get(`${prefix(workspaceId, provider)}/bindings`)
  const items = Array.isArray(res?.data?.items) ? res.data.items : res?.data?.data ?? []
  return normalizeBindingList(items)
}

async function getLast({ workspaceId, provider = DEFAULT_PROVIDER, authId, domain }) {
  if (!authId) throw new Error('authId is required')
  const url = `${prefix(workspaceId, provider)}/bindings/${encodeURIComponent(authId)}/sync/${domain}/last`
  const res = await http.get(url)
  const data = res?.data ?? {}
  return {
    status: data.status ?? data.state ?? null,
    triggeredAt: data.triggered_at ?? data.triggeredAt ?? null,
    finishedAt: data.finished_at ?? data.finishedAt ?? null,
    durationSec: parseNumber(data.duration_sec ?? data.duration ?? null),
    nextAllowedAt: data.next_allowed_at ?? data.nextAllowedAt ?? null,
    summary: parseSummary(data.summary ?? data.last_summary ?? null),
  }
}

async function postSync({
  workspaceId,
  provider = DEFAULT_PROVIDER,
  domain,
  authIds = [],
  payload = {},
} = {}) {
  const ids = Array.from(new Set(authIds.map(toId))).filter(Boolean)
  if (!domain) throw new Error('domain is required')
  if (!ids.length) throw new Error('authIds is required')
  const base = `${prefix(workspaceId, provider)}/bindings`
  const results = []

  if (ids.length > 1) {
    try {
      const res = await http.post(`${base}/sync/${encodeURIComponent(domain)}`, {
        ...payload,
        auth_ids: ids,
      })
      results.push({ authId: null, data: res?.data ?? null })
      return { mode: 'batch', results }
    } catch (err) {
      if (!err?.status || ![404, 405, 422].includes(err.status)) {
        throw err
      }
      // fall back to serial posts
    }
  }

  for (const id of ids) {
    const res = await http.post(
      `${base}/${encodeURIComponent(id)}/sync/${encodeURIComponent(domain)}`,
      payload
    )
    results.push({ authId: id, data: res?.data ?? null })
  }

  return { mode: 'serial', results }
}

const DEFAULT_DELAYS = [1000, 2000, 4000, 8000, 8000]

function isTerminalStatus(status) {
  if (!status) return false
  const v = String(status).toLowerCase()
  return ['success', 'succeeded', 'completed', 'done', 'ok', 'failed', 'error'].some((token) =>
    v.includes(token)
  )
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function pollLastUntilSettled({
  workspaceId,
  provider = DEFAULT_PROVIDER,
  domain,
  authIds = [],
  onUpdate,
  getLastFn = getLast,
  delays = DEFAULT_DELAYS,
} = {}) {
  const ids = Array.from(new Set(authIds.map(toId))).filter(Boolean)
  const results = new Map()
  const errors = []
  for (let attempt = 0; attempt < delays.length; attempt += 1) {
    let allTerminal = true
    for (const id of ids) {
      try {
        const data = await getLastFn({ workspaceId, provider, domain, authId: id })
        results.set(id, data)
        if (onUpdate) onUpdate({ authId: id, data })
        if (!isTerminalStatus(data?.status)) {
          allTerminal = false
        }
      } catch (error) {
        errors.push({ authId: id, error })
      }
    }
    if (allTerminal || !ids.length) {
      return {
        status: 'settled',
        attempts: attempt + 1,
        results: Object.fromEntries(results),
        errors,
      }
    }
    if (attempt === delays.length - 1) break
    await wait(delays[attempt])
  }
  return {
    status: 'timeout',
    attempts: delays.length,
    results: Object.fromEntries(results),
    errors,
  }
}

const syncApi = { listBindings, getLast, postSync, pollLastUntilSettled }

export default syncApi
export { listBindings, getLast, postSync, pollLastUntilSettled, normalizeBindingList, DEFAULT_DELAYS }

