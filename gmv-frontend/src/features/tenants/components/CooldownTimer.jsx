import { useEffect, useMemo, useState } from 'react'
import clsx from 'classnames'

function parseUntil(until) {
  if (!until) return null
  if (until instanceof Date && !Number.isNaN(until.getTime())) return until.getTime()
  const num = Number(until)
  if (Number.isFinite(num)) {
    if (num > 1e12) return num
    return Date.now() + num * 1000
  }
  const parsed = Date.parse(String(until))
  return Number.isNaN(parsed) ? null : parsed
}

function computeRemainingSeconds(until) {
  const ts = parseUntil(until)
  if (!ts) return 0
  const diff = Math.ceil((ts - Date.now()) / 1000)
  return diff > 0 ? diff : 0
}

export default function CooldownTimer({ until, onExpire, className }) {
  const [remaining, setRemaining] = useState(() => computeRemainingSeconds(until))

  useEffect(() => {
    setRemaining(computeRemainingSeconds(until))
    if (!until) return () => {}
    let active = true
    const tick = () => {
      if (!active) return
      const next = computeRemainingSeconds(until)
      setRemaining(next)
      if (next <= 0) {
        active = false
        if (onExpire) onExpire()
      }
    }
    const id = setInterval(tick, 1000)
    tick()
    return () => {
      active = false
      clearInterval(id)
    }
  }, [until, onExpire])

  const formatted = useMemo(() => {
    if (remaining <= 0) return null
    return `${remaining} 秒后可重试`
  }, [remaining])

  if (!formatted) return null

  return (
    <span className={clsx('cooldown-timer', className)} role="timer" aria-live="polite">
      {formatted}
    </span>
  )
}

export { computeRemainingSeconds }

