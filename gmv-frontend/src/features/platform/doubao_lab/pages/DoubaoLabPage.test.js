import { describe, expect, it } from 'vitest'
import {
  accountProxyLabel,
  authenticationLabel,
  capacityLabel,
  membershipLabel,
  networkLabel,
  poolStatusLabel,
} from './DoubaoLabPage.jsx'

describe('Doubao observed capacity label', () => {
  it('does not fabricate a numeric balance when the provider exposes none', () => {
    expect(capacityLabel({ state: 'unknown', reported_credits: null })).toContain(
      '官网未提供精确额度',
    )
  })

  it('surfaces the bounded retry state after a real quota failure', () => {
    expect(
      capacityLabel({ state: 'exhausted', retry_at: '2026-07-27T06:00:00Z' }),
    ).toContain('额度暂不可用')
  })
})

describe('Doubao account proxy presentation', () => {
  it('shows device-local direct mode without fabricating a proxy', () => {
    expect(accountProxyLabel({ network_mode: 'direct', proxy_id: null }, [])).toBe(
      '设备本地直连',
    )
  })

  it('shows the proxy name and redacted endpoint instead of a database id', () => {
    expect(accountProxyLabel(
      { proxy_id: 6 },
      [{ id: 6, name: 'Flow Proxy 04', display_url: 'socks5h://192.168.1.24:7893', is_active: true }],
    )).toBe('Flow Proxy 04 · socks5h://192.168.1.24:7893')
  })

  it('marks a bound proxy that has since been disabled', () => {
    expect(accountProxyLabel(
      { proxy_id: 3 },
      [{ id: 3, name: 'Flow Proxy 03', display_url: 'socks5h://192.168.1.23:7893', is_active: false }],
    )).toContain('已停用')
  })
})

describe('Doubao membership presentation', () => {
  it('fails closed to the free account limit', () => {
    expect(membershipLabel(undefined)).toBe('免费账号 · 最长 10 秒')
    expect(membershipLabel({ tier: 'free' })).toContain('10 秒')
  })

  it('shows the paid-only duration only after explicit marking', () => {
    expect(membershipLabel({ tier: 'enhanced' })).toBe('加强套餐 · 最长 15 秒')
  })
})

describe('Doubao layered account health presentation', () => {
  it('does not call a captured profile production-ready before strong auth and capability checks', () => {
    expect(poolStatusLabel({ status: 'auth_check_due', ready: false })).toBe('待认证探测')
    expect(poolStatusLabel({ status: 'capability_check_due', ready: false })).toContain('待视频能力验证')
  })

  it('separates authentication from network reachability', () => {
    expect(authenticationLabel({ state: 'authenticated', fresh: true })).toBe('已登录')
    expect(authenticationLabel({ state: 'auth_required', fresh: false })).toBe('登录已失效')
    expect(networkLabel({ state: 'region_restricted' })).toBe('区域受限')
    expect(networkLabel({ state: 'reachable' })).toBe('可访问')
  })

  it('does not present cached account health as usable when its Bridge is offline', () => {
    expect(poolStatusLabel({ status: 'device_offline', ready: false })).toBe('设备离线')
    expect(poolStatusLabel({ status: 'device_recovering', ready: false })).toBe('设备恢复中')
  })
})
