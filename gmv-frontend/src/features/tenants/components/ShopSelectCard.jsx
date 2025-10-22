import clsx from 'classnames'
import StatusBadge from './StatusBadge.jsx'
import CooldownTimer from './CooldownTimer.jsx'

function DiffRow({ diff }) {
  if (!diff) return null
  const { added = 0, removed = 0, updated = 0 } = diff
  return (
    <div className="diff-chip-row">
      <span className="diff-chip diff-chip--add">+新增 {added}</span>
      <span className="diff-chip diff-chip--remove">-减少 {removed}</span>
      <span className="diff-chip diff-chip--update">~更新 {updated}</span>
    </div>
  )
}

function formatAbsolute(value) {
  if (!value) return '--'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '--' : date.toLocaleString()
}

export default function ShopSelectCard({
  items = [],
  selectedIds = [],
  activeBcIds = [],
  activeAdvIds = [],
  bcMap = {},
  advMap = {},
  onToggle,
  onSelectAll,
  onClear,
  showOnlySelected,
  onToggleShowOnlySelected,
  onSync,
  loading,
  cooldownUntil,
  cooldownMap = {},
  lastByAuth = {},
  onShowDetail,
  syncDisabled,
}) {
  const filteredByBc = items.filter((shop) => activeBcIds.includes(shop.bcId))
  const filtered = showOnlySelected
    ? filteredByBc.filter((item) => selectedIds.includes(item.id))
    : filteredByBc

  const buttonDisabled =
    syncDisabled || loading || filteredByBc.length === 0 || Boolean(cooldownUntil)

  return (
    <section className="card data-card" aria-labelledby="shop-card-heading">
      <header className="data-card__header">
        <div>
          <h2 id="shop-card-heading">Shops</h2>
          <p className="small-muted">用于控制商品范围，可多选</p>
        </div>
        <div className="data-card__actions">
          <CooldownTimer until={cooldownUntil} />
          <button
            type="button"
            className="btn"
            disabled={buttonDisabled}
            onClick={onSync}
          >
            {loading ? '同步中…' : 'Sync Shops'}
          </button>
        </div>
      </header>

      <div className="data-card__toolbar">
        <div className="btn-group">
          <button type="button" className="btn ghost" onClick={onSelectAll}>
            全选
          </button>
          <button type="button" className="btn ghost" onClick={onClear}>
            清空
          </button>
        </div>
        <label className="toggle">
          <input
            type="checkbox"
            checked={!!showOnlySelected}
            onChange={onToggleShowOnlySelected}
          />
          <span>仅显示已选择</span>
        </label>
      </div>

      <div className="entity-list" role="list">
        {filteredByBc.length === 0 && (
          <div className="empty-state">请选择 Business Center 以查看店铺。</div>
        )}
        {filtered.length === 0 && filteredByBc.length > 0 && (
          <div className="empty-state">暂无匹配的店铺。</div>
        )}
        {filtered.map((shop) => {
          const selected = selectedIds.includes(shop.id)
          const last = lastByAuth?.[shop.authId] || null
          const summary = shop.summary ?? last?.summary ?? {}
          const cooldown = cooldownMap[shop.authId]
          const adv = advMap[shop.advertiserId]
          const bc = bcMap[shop.bcId]
          const disabledByAdv = activeAdvIds.length > 0 && !activeAdvIds.includes(shop.advertiserId)
          return (
            <div
              key={shop.id}
              className={clsx('entity-row', selected && 'is-selected', disabledByAdv && 'is-muted')}
              role="listitem"
            >
              <button
                type="button"
                className="entity-toggle"
                aria-pressed={selected}
                onClick={() => onToggle?.(shop.id)}
                disabled={disabledByAdv}
              >
                <div className="entity-toggle__title">
                  <span>{shop.name}</span>
                  {adv && <span className="entity-alias">广告主 {adv.name}</span>}
                  {bc && <span className="entity-alias">所属 {bc.name}</span>}
                </div>
                <StatusBadge
                  status={last?.status}
                  nextAllowedAt={last?.nextAllowedAt ?? cooldown}
                  finishedAt={last?.finishedAt}
                />
              </button>
              <div className="entity-meta">
                <div className="entity-counts">
                  <span>本地 {summary?.localCount ?? 0}</span>
                  {summary?.remoteCount !== undefined && summary?.remoteCount !== null && (
                    <span>远端 {summary.remoteCount}</span>
                  )}
                </div>
                <DiffRow diff={summary?.diff} />
                <div className="entity-footer">
                  <span className="small-muted">
                    最近：{formatAbsolute(last?.finishedAt) ?? '--'}
                  </span>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => onShowDetail?.(shop.authId)}
                  >
                    查看详情
                  </button>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

