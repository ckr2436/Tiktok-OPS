import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import {
  authorizationLabel,
  authorizationState,
  currentFlowFailureCode,
  isRoutable,
  isImportSessionComplete,
  profileCapacityText,
  stateClass,
} from './FlowAccountPoolPage.jsx'

describe('Flow account current health', () => {
  it('offers automatic-update status and a device-scoped manual EXE fallback', () => {
    const source = fs.readFileSync(
      path.join(process.cwd(), 'src/features/platform/flow2api/pages/FlowAccountPoolPage.jsx'),
      'utf8',
    )
    expect(source).toContain('device.agent_update_required')
    expect(source).toContain('正在自动更新')
    expect(source).toContain('自动更新不可用')
    expect(source).toContain('downloadBridgeAgent({')
    expect(source).toContain('下载最新版 EXE')
  })

  it('does not present a recovered historical failure as a current error', () => {
    const account = {
      is_active: true,
      routable: true,
      has_at: true,
      at_expires: '2027-07-29T12:00:00Z',
      last_failure_code: 'session_body',
      last_keepalive_status: 'success',
      keepalive_failure_count: 0,
      last_keepalive_success_at: '2026-07-26T09:24:00Z',
    }

    expect(currentFlowFailureCode(account)).toBe('')
    expect(stateClass(account)).toBe('is-healthy')
  })

  it('keeps a current keepalive failure visible', () => {
    const account = {
      is_active: true,
      last_failure_code: 'session_body',
      last_keepalive_status: 'failed',
      keepalive_failure_count: 2,
    }

    expect(currentFlowFailureCode(account)).toBe('session_body')
    expect(stateClass(account)).toBe('is-warning')
  })

  it('keeps an account ban authoritative even after a healthy keepalive', () => {
    const account = {
      is_active: true,
      ban_reason: 'GRANT_EXPIRED',
      last_keepalive_status: 'success',
      keepalive_failure_count: 0,
    }

    expect(stateClass(account)).toBe('is-warning')
    expect(isRoutable(account)).toBe(false)
  })

  it('does not count an active account with an expired grant as routable', () => {
    const now = Date.parse('2026-07-29T12:00:00Z')
    const account = {
      is_active: true,
      routable: true,
      has_at: true,
      at_expires: '2026-07-29T11:59:59Z',
    }

    expect(authorizationState(account, now)).toBe('expired')
    expect(authorizationLabel(account, now)).toBe('授权已过期，等待续期')
    expect(isRoutable(account, now)).toBe(false)
  })

  it('shows a near-expiry grant as refresh due while it remains routable', () => {
    const now = Date.parse('2026-07-29T12:00:00Z')
    const account = {
      is_active: true,
      routable: true,
      has_at: true,
      at_expires: '2026-07-29T12:15:00Z',
    }

    expect(authorizationState(account, now)).toBe('refresh_due')
    expect(isRoutable(account, now)).toBe(true)
  })

  it('shows shared persistent profile usage by workload', () => {
    expect(profileCapacityText({
      profile_used_count: 32,
      profile_capacity: 64,
      profile_usage: { flow: 10, doubao: 16, jimeng: 1, yt_dlp: 0, content: 5 },
    })).toBe('Profile 32/64 · Flow 10 · 豆包 16 · 即梦 1 · 下载 0 · 内容 5')
  })

  it('collapses a completed account import to one close action', () => {
    expect(isImportSessionComplete({ state: 'ready' })).toBe(true)
    expect(isImportSessionComplete({ state: 'capture_pending' })).toBe(false)
    expect(isImportSessionComplete(null)).toBe(false)
  })
})
