import { useCallback, useMemo } from 'react'
import { formatRelative, parseTimestamp } from './StatusBadge.jsx'

function formatDuration(sec) {
  if (sec === undefined || sec === null) return '--'
  const value = Number(sec)
  if (!Number.isFinite(value)) return '--'
  if (value < 60) return `${Math.round(value)} 秒`
  if (value < 3600) return `${(value / 60).toFixed(1)} 分钟`
  return `${(value / 3600).toFixed(1)} 小时`
}

function DiffChips({ diff }) {
  if (!diff) return null
  const { added = 0, removed = 0, updated = 0 } = diff
  return (
    <div className="diff-chip-row" aria-label="差异概览">
      <span className="diff-chip diff-chip--add">+新增 {added}</span>
      <span className="diff-chip diff-chip--remove">-减少 {removed}</span>
      <span className="diff-chip diff-chip--update">~更新 {updated}</span>
    </div>
  )
}

export default function LastResultDialog({ open, result, requestId, onClose, domain }) {
  const summary = result?.summary ?? {}
  const triggered = parseTimestamp(result?.triggeredAt)
  const finished = parseTimestamp(result?.finishedAt)
  const relative = formatRelative(result?.finishedAt)

  const handleCopy = useCallback(() => {
    if (!requestId) return
    if (navigator?.clipboard?.writeText) {
      navigator.clipboard.writeText(String(requestId)).catch(() => {})
    }
  }, [requestId])

  const title = useMemo(() => {
    if (!domain) return '最近同步结果'
    return `最近同步结果 · ${domain.toUpperCase()}`
  }, [domain])

  if (!open) return null

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <header className="modal__header">
          <h3 className="modal__title">{title}</h3>
          <button type="button" className="modal__close" onClick={onClose}>
            关闭
          </button>
        </header>
        <div className="modal__body last-result-dialog">
          <dl className="result-grid">
            <div>
              <dt>状态</dt>
              <dd>{result?.status ?? '--'}</dd>
            </div>
            <div>
              <dt>开始时间</dt>
              <dd>{triggered ? triggered.toLocaleString() : '--'}</dd>
            </div>
            <div>
              <dt>结束时间</dt>
              <dd>{finished ? finished.toLocaleString() : '--'}</dd>
            </div>
            <div>
              <dt>相对时间</dt>
              <dd>{relative ?? '--'}</dd>
            </div>
            <div>
              <dt>耗时</dt>
              <dd>{formatDuration(result?.durationSec)}</dd>
            </div>
            <div>
              <dt>本地数量</dt>
              <dd>{summary?.localCount ?? '--'}</dd>
            </div>
            <div>
              <dt>远端数量</dt>
              <dd>{summary?.remoteCount ?? '--'}</dd>
            </div>
            <div className="result-grid__full">
              <dt>差异</dt>
              <dd>
                <DiffChips diff={summary?.diff} />
              </dd>
            </div>
          </dl>

          {requestId && (
            <div className="request-id-row">
              <span className="small-muted">X-Request-ID</span>
              <code>{requestId}</code>
              <button type="button" className="btn ghost" onClick={handleCopy}>
                复制
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

