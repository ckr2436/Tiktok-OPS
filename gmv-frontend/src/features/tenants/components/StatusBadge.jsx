import clsx from 'classnames'

function normalizeStatus(status) {
  if (!status) return ''
  return String(status).toLowerCase()
}

function isSuccess(status) {
  const value = normalizeStatus(status)
  return ['success', 'succeeded', 'completed', 'done', 'ok', 'synced'].some((token) =>
    value.includes(token)
  )
}

function isFailure(status) {
  const value = normalizeStatus(status)
  return ['fail', 'error', 'denied', 'timeout'].some((token) => value.includes(token))
}

function parseTimestamp(value) {
  if (!value) return null
  if (value instanceof Date && !Number.isNaN(value.getTime())) return value
  const asNumber = Number(value)
  if (Number.isFinite(asNumber)) {
    if (asNumber > 1e12) return new Date(asNumber)
    return new Date(asNumber * 1000)
  }
  const parsed = new Date(String(value))
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function formatRelative(value) {
  const date = parseTimestamp(value)
  if (!date) return null
  const diffMs = Date.now() - date.getTime()
  const abs = Math.abs(diffMs)
  if (abs < 60_000) return '刚刚'
  if (abs < 3_600_000) return `${Math.round(abs / 60_000)} 分钟前`
  if (abs < 86_400_000) return `${Math.round(abs / 3_600_000)} 小时前`
  return `${Math.round(abs / 86_400_000)} 天前`
}

function determineState({ status, nextAllowedAt }) {
  const cooldownTarget = parseTimestamp(nextAllowedAt)
  if (cooldownTarget && cooldownTarget.getTime() > Date.now()) {
    return { className: 'is-cooldown', icon: '🔵', label: 'Cooldown' }
  }
  if (isSuccess(status)) {
    return { className: 'is-ok', icon: '✅', label: 'Up-to-date' }
  }
  if (isFailure(status)) {
    return { className: 'is-failed', icon: '🔴', label: 'Failed last' }
  }
  return { className: 'is-stale', icon: '🟡', label: 'Needs sync' }
}

export default function StatusBadge({ status, nextAllowedAt, finishedAt, className }) {
  const state = determineState({ status, nextAllowedAt })
  const relative = formatRelative(finishedAt)
  const absolute = parseTimestamp(finishedAt)
  const titleParts = []
  if (status) titleParts.push(`状态: ${status}`)
  if (absolute) titleParts.push(`完成时间: ${absolute.toLocaleString()}`)
  const title = titleParts.join('\n')

  return (
    <span
      className={clsx('status-badge', state.className, className)}
      aria-live="polite"
      title={title || undefined}
    >
      <span className="status-badge__icon" aria-hidden="true">
        {state.icon}
      </span>
      <span>{state.label}</span>
      {relative && <span className="status-badge__meta"> · {relative}</span>}
    </span>
  )
}

export { parseTimestamp, formatRelative }

