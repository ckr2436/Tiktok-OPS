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

export default function AdvSelectCard({
  items = [],
  selectedIds = [],
  activeBcIds = [],
  bcMap = {},
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
  const filteredByBc = items.filter((adv) => activeBcIds.includes(adv.bcId))
  const filtered = showOnlySelected
    ? filteredByBc.filter((item) => selectedIds.includes(item.id))
    : filteredByBc

  const buttonDisabled =
    syncDisabled || loading || filteredByBc.length === 0 || Boolean(cooldownUntil)

  return (
    <section className="card data-card" aria-labelledby="adv-card-heading">
      <header className="data-card__header">
        <div>
          <h2 id="adv-card-heading">Advertisers</h2>
          <p className="small-muted">从所选 BC 级联，可进一步细分同步范围</p>
        </div>
        <div className="data-card__actions">
          <CooldownTimer until={cooldownUntil} />
          <button
            type="button"
            className="btn"
            disabled={buttonDisabled}
            onClick={onSync}
          >
            {loading ? '同步中…' : 'Sync Advertisers'}
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
          <div className="empty-state">请选择 Business Center 以查看广告主。</div>
        )}
        {filtered.length === 0 && filteredByBc.length > 0 && (
          <div className="empty-state">暂无匹配的广告主。</div>
        )}
        {filtered.map((adv) => {
          const selected = selectedIds.includes(adv.id)
          const last = lastByAuth?.[adv.authId] || null
          const summary = adv.summary ?? last?.summary ?? {}
          const cooldown = cooldownMap[adv.authId]
          const bc = bcMap[adv.bcId]
          return (
            <div
              key={adv.id}
              className={clsx('entity-row', selected && 'is-selected')}
              role="listitem"
            >
              <button
                type="button"
                className="entity-toggle"
                aria-pressed={selected}
                onClick={() => onToggle?.(adv.id)}
              >
                <div className="entity-toggle__title">
                  <span>{adv.name}</span>
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
                    onClick={() => onShowDetail?.(adv.authId)}
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

