import { useMemo } from 'react'
import clsx from 'classnames'
import CooldownTimer from './CooldownTimer.js'
import StatusBadge, { formatRelative as formatStatusRelative, parseTimestamp } from './StatusBadge.jsx'

function formatAbsoluteTime(value) {
  if (!value) return '--'
  const parsed = parseTimestamp(value)
  if (!parsed) return '--'
  return parsed.toLocaleString()
}

function isSuccess(status) {
  if (!status) return false
  const value = String(status).toLowerCase()
  return ['success', 'succeeded', 'completed', 'done', 'ok'].some((token) => value.includes(token))
}

function isFailure(status) {
  if (!status) return false
  const value = String(status).toLowerCase()
  return ['fail', 'failed', 'error', 'denied'].some((token) => value.includes(token))
}

function isRunning(status) {
  if (!status) return false
  const value = String(status).toLowerCase()
  return ['running', 'processing', 'pending', 'syncing', 'working'].some((token) =>
    value.includes(token)
  )
}

function aggregateSummaryDiff(summary) {
  if (!summary) return { added: 0, removed: 0, updated: 0 }
  const diff = summary.diff ?? {}
  return {
    added: Number(diff.added ?? 0),
    removed: Number(diff.removed ?? 0),
    updated: Number(diff.updated ?? 0),
  }
}

function addDiffTotals(target, source) {
  target.added += Number(source.added ?? 0)
  target.removed += Number(source.removed ?? 0)
  target.updated += Number(source.updated ?? 0)
}

function computeDomainMeta({ authIds = [], lastByAuth = {}, fallbackItems = [], cooldownMap = {} }) {
  const ids = Array.from(new Set((authIds || []).filter(Boolean)))
  const fallbackMap = new Map()
  fallbackItems.forEach((item) => {
    if (item?.authId) fallbackMap.set(String(item.authId), item)
  })

  const totals = { added: 0, removed: 0, updated: 0 }
  let earliestNext = null
  let latestFinished = null
  let successCount = 0
  let failureCount = 0
  let runningCount = 0
  let hasData = false

  function consider(id, entry, fallback) {
    if (!entry && !fallback) return
    hasData = true
    const lastInfo = entry || fallback?.last || fallback || {}
    const summarySource = entry?.summary ?? fallback?.summary ?? fallback?.last?.summary ?? null
    addDiffTotals(totals, aggregateSummaryDiff(summarySource))

    const status = lastInfo.status ?? entry?.status ?? fallback?.status ?? null
    if (isFailure(status)) {
      failureCount += 1
    } else if (isSuccess(status)) {
      successCount += 1
    } else if (isRunning(status)) {
      runningCount += 1
    } else if (status) {
      runningCount += 1
    }

    const finishedAt = lastInfo.finishedAt ?? entry?.finishedAt ?? fallback?.finishedAt ?? null
    const finishedDate = parseTimestamp(finishedAt)
    if (finishedDate && (latestFinished === null || finishedDate.getTime() > latestFinished)) {
      latestFinished = finishedDate.getTime()
    }

    const nextAllowedCandidate =
      lastInfo.nextAllowedAt ?? entry?.nextAllowedAt ?? fallback?.nextAllowedAt ?? cooldownMap[id]
    const nextDate = parseTimestamp(nextAllowedCandidate)
    if (nextDate && nextDate.getTime() > Date.now()) {
      if (earliestNext === null || nextDate.getTime() < earliestNext) {
        earliestNext = nextDate.getTime()
      }
    }
  }

  if (ids.length === 0 && lastByAuth?.all) {
    consider('all', lastByAuth.all, fallbackMap.get('all'))
  } else {
    ids.forEach((id) => {
      consider(id, lastByAuth?.[id], fallbackMap.get(id))
    })
  }

  let status = null
  if (failureCount > 0) {
    status = 'failed'
  } else if (successCount > 0 && runningCount === 0) {
    status = 'success'
  } else if (runningCount > 0) {
    status = 'running'
  } else if (hasData) {
    status = 'unknown'
  }

  return {
    status,
    nextAllowedAt: earliestNext ? new Date(earliestNext).toISOString() : null,
    finishedAt: latestFinished ? new Date(latestFinished).toISOString() : null,
    diff: totals,
  }
}

function DiffChipRow({ diff }) {
  if (!diff) return null
  const { added = 0, removed = 0, updated = 0 } = diff
  if (added === 0 && removed === 0 && updated === 0) return null
  return (
    <div className="diff-chip-row">
      <span className="diff-chip diff-chip--add">+新增 {added}</span>
      <span className="diff-chip diff-chip--remove">-减少 {removed}</span>
      <span className="diff-chip diff-chip--update">~更新 {updated}</span>
    </div>
  )
}

function determineTone(status) {
  if (isFailure(status)) return 'is-failed'
  if (isSuccess(status)) return 'is-success'
  if (isRunning(status)) return 'is-running'
  return 'is-idle'
}

function SelectionChip({ id, label, secondary, selected, onToggle, onView, status, title }) {
  return (
    <div
      className={clsx('entity-chip', selected && 'is-selected')}
      data-status={determineTone(status)}
      role="listitem"
    >
      <button
        type="button"
        className="entity-chip__toggle"
        aria-pressed={selected}
        onClick={() => onToggle?.(id)}
        title={title || undefined}
      >
        <span className="entity-chip__dot" aria-hidden="true" />
        <span className="entity-chip__label">{label}</span>
        {secondary && <span className="entity-chip__secondary">{secondary}</span>}
      </button>
      {onView && (
        <button
          type="button"
          className="entity-chip__detail"
          aria-label={`查看 ${label} 最近同步详情`}
          onClick={onView}
        >
          🔍
        </button>
      )}
    </div>
  )
}

function SelectionRow({
  headingId,
  title,
  description,
  chips,
  selectedCount,
  totalCount,
  meta,
  syncLabel,
  onSelectAll,
  onClear,
  showOnlySelected,
  onToggleShowOnly,
  onSync,
  loading,
  cooldownUntil,
  onCooldownExpire,
  syncDisabled,
}) {
  const relative = meta?.finishedAt ? formatStatusRelative(meta.finishedAt) : null
  const absolute = formatAbsoluteTime(meta?.finishedAt)
  const disableButton =
    syncDisabled ||
    loading ||
    (meta?.nextAllowedAt && parseTimestamp(meta.nextAllowedAt)?.getTime() > Date.now())

  return (
    <section className="compact-row" aria-labelledby={headingId}>
      <div className="compact-row__main">
        <header className="compact-row__header">
          <div>
            <h3 id={headingId}>{title}</h3>
            {description && <p className="small-muted">{description}</p>}
          </div>
          <div className="compact-row__toolbar">
            <span className="compact-row__count">已选 {selectedCount} / {totalCount}</span>
            <div className="btn-group">
              <button type="button" className="btn ghost" onClick={onSelectAll}>
                全选
              </button>
              <button type="button" className="btn ghost" onClick={onClear}>
                清空
              </button>
            </div>
            <label className="toggle">
              <input type="checkbox" checked={!!showOnlySelected} onChange={onToggleShowOnly} />
              <span>仅显示已选</span>
            </label>
          </div>
        </header>
        <div className="compact-row__chips" role="list">
          {chips.length === 0 && <div className="empty-state">暂无数据</div>}
          {chips.map((chip) => (
            <SelectionChip key={chip.id} {...chip} />
          ))}
        </div>
      </div>
      <aside className="compact-row__side">
        <StatusBadge
          status={meta?.status}
          nextAllowedAt={meta?.nextAllowedAt ?? cooldownUntil}
          finishedAt={meta?.finishedAt}
        />
        <div className="compact-row__time">
          <strong>上次同步</strong>
          <span>{relative ? `${relative} · ${absolute}` : absolute}</span>
        </div>
        <DiffChipRow diff={meta?.diff} />
        <div className="compact-row__actions">
          <CooldownTimer until={meta?.nextAllowedAt ?? cooldownUntil} onExpire={onCooldownExpire} />
          <button type="button" className="btn" disabled={disableButton} onClick={onSync}>
            {loading ? '同步中…' : syncLabel}
          </button>
        </div>
      </aside>
    </section>
  )
}

export default function TenantSyncDashboardCard({
  bcList = [],
  advertisers = [],
  shops = [],
  bcMap = {},
  advMap = {},
  selectedBCIds = [],
  selectedAdvIds = [],
  selectedShopIds = [],
  showOnlySelected = {},
  onToggleBc,
  onToggleAdv,
  onToggleShop,
  onSelectAllBcs,
  onClearBcs,
  onSelectAllAdvs,
  onClearAdvs,
  onSelectAllShops,
  onClearShops,
  onToggleShowOnlyBc,
  onToggleShowOnlyAdv,
  onToggleShowOnlyShop,
  onSyncBc,
  onSyncAdvertisers,
  onSyncShops,
  loadingBc,
  loadingAdvertisers,
  loadingShops,
  cooldownBc,
  cooldownAdvertisers,
  cooldownShops,
  cooldownMapBc = {},
  cooldownMapAdvertisers = {},
  cooldownMapShops = {},
  lastByDomain = {},
  onShowDetail,
  getAuthIdsForDomain,
  onRefreshLast,
}) {
  const bcAuthIds = useMemo(() => getAuthIdsForDomain?.('bc') || [], [getAuthIdsForDomain, selectedBCIds])
  const advAuthIds = useMemo(
    () => getAuthIdsForDomain?.('advertisers') || [],
    [getAuthIdsForDomain, selectedAdvIds, selectedBCIds]
  )
  const shopAuthIds = useMemo(
    () => getAuthIdsForDomain?.('shops') || [],
    [getAuthIdsForDomain, selectedShopIds, selectedAdvIds, selectedBCIds]
  )

  const bcMeta = useMemo(
    () =>
      computeDomainMeta({
        authIds: bcAuthIds,
        lastByAuth: lastByDomain?.bc,
        fallbackItems: bcList,
        cooldownMap: cooldownMapBc,
      }),
    [bcAuthIds, lastByDomain, bcList, cooldownMapBc]
  )

  const advMeta = useMemo(
    () =>
      computeDomainMeta({
        authIds: advAuthIds,
        lastByAuth: lastByDomain?.advertisers,
        fallbackItems: advertisers,
        cooldownMap: cooldownMapAdvertisers,
      }),
    [advAuthIds, lastByDomain, advertisers, cooldownMapAdvertisers]
  )

  const shopMeta = useMemo(
    () =>
      computeDomainMeta({
        authIds: shopAuthIds,
        lastByAuth: lastByDomain?.shops,
        fallbackItems: shops,
        cooldownMap: cooldownMapShops,
      }),
    [shopAuthIds, lastByDomain, shops, cooldownMapShops]
  )

  const bcChips = useMemo(() => {
    const base = showOnlySelected?.bc
      ? bcList.filter((bc) => selectedBCIds.includes(bc.id))
      : bcList
    return base.map((bc) => {
      const last = lastByDomain?.bc?.[bc.authId] ?? bc.last ?? null
      const label = bc.name || bc.alias || bc.id
      const secondary = bc.alias && bc.alias !== bc.name ? bc.alias : null
      const titleParts = [label]
      if (bc.alias && secondary) titleParts.push(`别名: ${bc.alias}`)
      if (bc.authId) titleParts.push(`Auth: ${bc.authId}`)
      return {
        id: bc.id,
        label,
        secondary,
        selected: selectedBCIds.includes(bc.id),
        onToggle: onToggleBc,
        onView: bc.authId ? () => onShowDetail?.('bc', bc.authId) : null,
        status: last?.status ?? bc.status,
        title: titleParts.join('\n'),
      }
    })
  }, [bcList, showOnlySelected, selectedBCIds, lastByDomain, onToggleBc, onShowDetail])

  const advChips = useMemo(() => {
    const scoped = advertisers.filter((adv) => selectedBCIds.includes(adv.bcId))
    const base = showOnlySelected?.advertisers
      ? scoped.filter((adv) => selectedAdvIds.includes(adv.id))
      : scoped
    return base.map((adv) => {
      const last = lastByDomain?.advertisers?.[adv.authId] ?? adv.last ?? null
      const bcName = bcMap[adv.bcId]?.name ?? adv.bcId
      const titleParts = [adv.name]
      if (bcName) titleParts.push(`所属 BC: ${bcName}`)
      return {
        id: adv.id,
        label: adv.name,
        secondary: bcName,
        selected: selectedAdvIds.includes(adv.id),
        onToggle: onToggleAdv,
        onView: adv.authId ? () => onShowDetail?.('advertisers', adv.authId) : null,
        status: last?.status ?? adv.status,
        title: titleParts.join('\n'),
      }
    })
  }, [
    advertisers,
    selectedBCIds,
    showOnlySelected,
    selectedAdvIds,
    lastByDomain,
    onToggleAdv,
    onShowDetail,
    bcMap,
  ])

  const shopChips = useMemo(() => {
    const scoped = shops.filter((shop) => selectedBCIds.includes(shop.bcId))
    const filtered = selectedAdvIds.length
      ? scoped.filter((shop) => selectedAdvIds.includes(shop.advertiserId))
      : scoped
    const base = showOnlySelected?.shops
      ? filtered.filter((shop) => selectedShopIds.includes(shop.id))
      : filtered
    return base.map((shop) => {
      const last = lastByDomain?.shops?.[shop.authId] ?? shop.last ?? null
      const advName = advMap[shop.advertiserId]?.name ?? shop.advertiserId
      return {
        id: shop.id,
        label: shop.name,
        secondary: advName,
        selected: selectedShopIds.includes(shop.id),
        onToggle: onToggleShop,
        onView: shop.authId ? () => onShowDetail?.('shops', shop.authId) : null,
        status: last?.status ?? shop.status,
        title: `${shop.name}\n所属广告主: ${advName}`,
      }
    })
  }, [
    shops,
    selectedBCIds,
    selectedAdvIds,
    showOnlySelected,
    selectedShopIds,
    lastByDomain,
    onToggleShop,
    onShowDetail,
    advMap,
  ])

  return (
    <section className="card data-card compact-dashboard" aria-label="同步控制台">
      <header className="data-card__header">
        <div>
          <h2>账户同步仪表盘</h2>
          <p className="small-muted">在同一处管理 Business Center / Advertiser / Shop 选择与同步</p>
        </div>
      </header>
      <div className="compact-dashboard__rows">
        <SelectionRow
          headingId="bc-compact-heading"
          title="Business Center"
          description="选择范围将联动下级实体"
          chips={bcChips}
          selectedCount={selectedBCIds.length}
          totalCount={bcList.length}
          meta={bcMeta}
          syncLabel="Sync BC"
          onSelectAll={onSelectAllBcs}
          onClear={onClearBcs}
          showOnlySelected={showOnlySelected?.bc}
          onToggleShowOnly={onToggleShowOnlyBc}
          onSync={onSyncBc}
          loading={loadingBc}
          cooldownUntil={cooldownBc}
          onCooldownExpire={() => onRefreshLast?.('bc', bcAuthIds)}
          syncDisabled={bcAuthIds.length === 0}
        />
        <SelectionRow
          headingId="adv-compact-heading"
          title="Advertisers"
          description="仅展示已选择 BC 的广告主"
          chips={advChips}
          selectedCount={selectedAdvIds.length}
          totalCount={advertisers.filter((adv) => selectedBCIds.includes(adv.bcId)).length}
          meta={advMeta}
          syncLabel="Sync Advertisers"
          onSelectAll={onSelectAllAdvs}
          onClear={onClearAdvs}
          showOnlySelected={showOnlySelected?.advertisers}
          onToggleShowOnly={onToggleShowOnlyAdv}
          onSync={onSyncAdvertisers}
          loading={loadingAdvertisers}
          cooldownUntil={cooldownAdvertisers}
          onCooldownExpire={() => onRefreshLast?.('advertisers', advAuthIds)}
          syncDisabled={advAuthIds.length === 0}
        />
        <SelectionRow
          headingId="shop-compact-heading"
          title="Shops"
          description="基于所选广告主过滤店铺"
          chips={shopChips}
          selectedCount={selectedShopIds.length}
          totalCount={shops.filter((shop) => selectedBCIds.includes(shop.bcId)).length}
          meta={shopMeta}
          syncLabel="Sync Shops"
          onSelectAll={onSelectAllShops}
          onClear={onClearShops}
          showOnlySelected={showOnlySelected?.shops}
          onToggleShowOnly={onToggleShowOnlyShop}
          onSync={onSyncShops}
          loading={loadingShops}
          cooldownUntil={cooldownShops}
          onCooldownExpire={() => onRefreshLast?.('shops', shopAuthIds)}
          syncDisabled={shopAuthIds.length === 0}
        />
      </div>
    </section>
  )
}

export { computeDomainMeta, formatAbsoluteTime }
