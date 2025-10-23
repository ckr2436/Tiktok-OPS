import clsx from 'classnames'
import StatusBadge from './StatusBadge.jsx'
import CooldownTimer from './CooldownTimer.js'

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

export default function BCSelectCard({
  items = [],
  selectedIds = [],
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
  const filtered = showOnlySelected
    ? items.filter((item) => selectedIds.includes(item.id))
    : items

  const buttonDisabled =
    syncDisabled || loading || selectedIds.length === 0 || Boolean(cooldownUntil)

  return (
    <section className="card data-card" aria-labelledby="bc-card-heading">
      <header className="data-card__header">
        <div>
          <h2 id="bc-card-heading">Business Center</h2>
          <p className="small-muted">作为级联来源，勾选后会预选其下级实体</p>
        </div>
        <div className="data-card__actions">
          <CooldownTimer until={cooldownUntil} />
          <button
            type="button"
            className="btn"
            disabled={buttonDisabled}
            onClick={onSync}
          >
            {loading ? '同步中…' : 'Sync BC'}
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
        {filtered.length === 0 && (
          <div className="empty-state">暂无绑定，请先完成授权。</div>
        )}
        {filtered.map((bc) => {
          const selected = selectedIds.includes(bc.id)
          const last = lastByAuth?.[bc.authId] || null
          const summary = bc.summary ?? last?.summary ?? {}
          const cooldown = cooldownMap[bc.authId]
          return (
            <div
              key={bc.id}
              className={clsx('entity-row', selected && 'is-selected')}
              role="listitem"
            >
              <button
                type="button"
                className="entity-toggle"
                aria-pressed={selected}
                onClick={() => onToggle?.(bc.id)}
              >
                <div className="entity-toggle__title">
                  <span>{bc.name}</span>
                  {bc.alias && <span className="entity-alias">{bc.alias}</span>}
                  {bc.authExpiresAt && (
                    <span className="entity-badge">授权截至 {formatAbsolute(bc.authExpiresAt)}</span>
                  )}
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
                    onClick={() => onShowDetail?.(bc.authId)}
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

