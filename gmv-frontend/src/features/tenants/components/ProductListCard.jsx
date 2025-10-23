import { useMemo } from 'react'
import StatusBadge from './StatusBadge.jsx'
import CooldownTimer from './CooldownTimer.js'
import { filterProducts } from '../utils/productFilters.js'

function formatAbsolute(value) {
  if (!value) return '--'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '--' : date.toLocaleString()
}

function deriveProductState(product) {
  const change = String(product?.changeType || '').toLowerCase()
  if (change.includes('add')) return { label: '新增', className: 'tag-add' }
  if (change.includes('remove') || change.includes('delete')) return { label: '删除', className: 'tag-remove' }
  if (change.includes('update') || change.includes('modify')) return { label: '更新', className: 'tag-update' }
  const status = String(product?.status || '').toLowerCase()
  if (status.includes('fail') || status.includes('error')) return { label: '失败', className: 'tag-fail' }
  return { label: '无变化', className: 'tag-neutral' }
}

function aggregateStatus(authIds, lastByAuth) {
  if (!authIds.length) return { status: null, nextAllowedAt: null, finishedAt: null }
  let hasFailure = false
  let hasSuccess = true
  let nextAllowedAt = null
  let latestFinished = null
  for (const id of authIds) {
    const entry = lastByAuth?.[id]
    if (!entry) {
      hasSuccess = false
      continue
    }
    const status = String(entry.status || '').toLowerCase()
    if (status.includes('fail') || status.includes('error')) hasFailure = true
    if (!status.includes('success') && !status.includes('done') && !status.includes('completed')) {
      hasSuccess = false
    }
    if (entry.nextAllowedAt) {
      const candidate = Date.parse(entry.nextAllowedAt)
      if (!Number.isNaN(candidate)) {
        if (nextAllowedAt === null || candidate < nextAllowedAt) {
          nextAllowedAt = candidate
        }
      }
    }
    if (entry.finishedAt) {
      const ts = Date.parse(entry.finishedAt)
      if (!Number.isNaN(ts)) {
        if (latestFinished === null || ts > latestFinished) latestFinished = ts
      }
    }
  }
  if (hasFailure) {
    return { status: 'failed', nextAllowedAt, finishedAt: latestFinished }
  }
  if (hasSuccess && authIds.length > 0) {
    return { status: 'success', nextAllowedAt, finishedAt: latestFinished }
  }
  return { status: null, nextAllowedAt, finishedAt: latestFinished }
}

export default function ProductListCard({
  products = [],
  shopMap = {},
  advMap = {},
  bcMap = {},
  selectedShopIds = [],
  selectedAdvIds = [],
  selectedBCIds = [],
  filters = {},
  onToggleFilter,
  onSync,
  loading,
  cooldownMap = {},
  lastByAuth = {},
  syncDisabled,
}) {
  const scopeAuthIds = useMemo(() => {
    const ids = new Set()
    if (selectedShopIds.length > 0) {
      selectedShopIds.forEach((id) => {
        const shop = shopMap[id]
        if (shop?.authId) ids.add(shop.authId)
      })
    } else if (selectedAdvIds.length > 0) {
      selectedAdvIds.forEach((id) => {
        const adv = advMap[id]
        if (adv?.authId) ids.add(adv.authId)
      })
    } else {
      selectedBCIds.forEach((id) => {
        const bc = bcMap[id]
        if (bc?.authId) ids.add(bc.authId)
      })
    }
    return Array.from(ids)
  }, [selectedShopIds, selectedAdvIds, selectedBCIds, shopMap, advMap, bcMap])

  const { status, nextAllowedAt, finishedAt } = useMemo(
    () => aggregateStatus(scopeAuthIds, lastByAuth),
    [scopeAuthIds, lastByAuth]
  )

  const filteredProducts = useMemo(
    () =>
      filterProducts({
        products,
        selectedShopIds,
        selectedAdvIds,
        selectedBCIds,
        filters,
      }),
    [products, selectedShopIds, selectedAdvIds, selectedBCIds, filters]
  )

  const cooldownUntil = useMemo(() => {
    if (!scopeAuthIds.length) return null
    let earliest = null
    for (const id of scopeAuthIds) {
      const value = cooldownMap[id]
      if (!value) continue
      const ts = Date.parse(value)
      if (Number.isNaN(ts)) continue
      if (earliest === null || ts < earliest) earliest = ts
    }
    return earliest
  }, [scopeAuthIds, cooldownMap])

  const buttonDisabled =
    syncDisabled || loading || scopeAuthIds.length === 0 || Boolean(cooldownUntil)

  return (
    <section className="card data-card" aria-labelledby="product-card-heading">
      <header className="data-card__header">
        <div>
          <h2 id="product-card-heading">Products</h2>
          <p className="small-muted">将按照选择范围触发商品同步</p>
        </div>
        <div className="data-card__actions">
          <CooldownTimer until={cooldownUntil || nextAllowedAt} />
          <StatusBadge status={status} nextAllowedAt={nextAllowedAt} finishedAt={finishedAt} />
          <button
            type="button"
            className="btn"
            disabled={buttonDisabled}
            onClick={onSync}
          >
            {loading ? '同步中…' : 'Sync Products'}
          </button>
        </div>
      </header>

      <div className="data-card__toolbar">
        <div className="filter-group">
          <label className="toggle">
            <input
              type="checkbox"
              checked={!!filters.onlyChanges}
              onChange={() => onToggleFilter?.('onlyChanges')}
            />
            <span>仅显示有变动</span>
          </label>
          <label className="toggle">
            <input
              type="checkbox"
              checked={!!filters.onlyFailed}
              onChange={() => onToggleFilter?.('onlyFailed')}
            />
            <span>仅显示失败</span>
          </label>
        </div>
      </div>

      <div className="product-table-wrapper">
        <table className="product-table">
          <thead>
            <tr>
              <th>商品ID / 标题</th>
              <th>所属店铺</th>
              <th>上次变更时间</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {filteredProducts.length === 0 && (
              <tr>
                <td colSpan={4} className="empty-cell">
                  暂无商品数据
                </td>
              </tr>
            )}
            {filteredProducts.map((product) => {
              const shop = shopMap[product.shopId]
              const adv = shop ? advMap[shop.advertiserId] : advMap[product.advertiserId]
              const state = deriveProductState(product)
              return (
                <tr key={product.id}>
                  <td>
                    <div className="product-title">
                      <strong>{product.id}</strong>
                      <span>{product.title}</span>
                    </div>
                  </td>
                  <td>
                    <div className="product-ref">
                      {shop ? <span>{shop.name}</span> : <span className="small-muted">未知店铺</span>}
                      {adv && <span className="small-muted"> · {adv.name}</span>}
                    </div>
                  </td>
                  <td>
                    <span>{formatAbsolute(product.lastChangedAt)}</span>
                  </td>
                  <td>
                    <span className={`status-tag ${state.className}`} title={product.failureReason || undefined}>
                      {state.label}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

