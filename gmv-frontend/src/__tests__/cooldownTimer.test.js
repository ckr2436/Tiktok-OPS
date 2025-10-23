import { describe, it, expect, beforeEach, afterEach, vi } from './testUtils.js'
import { computeRemainingSeconds } from '../features/tenants/components/CooldownTimer.js'

describe('CooldownTimer', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2025-01-01T00:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('parses retry-after style values', () => {
    expect(computeRemainingSeconds(10)).toBe(10)
    expect(computeRemainingSeconds('5')).toBe(5)
    expect(computeRemainingSeconds('2025-01-01T00:00:10Z')).toBe(10)
  })

  it('invokes onExpire once the countdown finishes', () => {
    const onExpire = vi.fn()
    const until = Date.now() + 3000
    const tick = () => {
      const remaining = computeRemainingSeconds(until)
      if (remaining <= 0) {
        clearInterval(timer)
        onExpire()
      }
    }
    const timer = setInterval(tick, 1000)
    tick()
    vi.advanceTimersByTime(3500)
    expect(onExpire).toHaveBeenCalledTimes(1)
    clearInterval(timer)
  })
})
