import { describe, expect, it } from 'vitest'

import {
  authorizationActionLabel,
  oauthCallbackErrorText,
} from './TiktokShopAuthList.jsx'


describe('oauthCallbackErrorText', () => {
  it('explains invalid application credentials without exposing provider details', () => {
    expect(
      oauthCallbackErrorText('TOKEN_EXCHANGE_FAILED', 'APP_CREDENTIALS_INVALID'),
    ).toContain('应用凭据无效或已变更')
  })

  it('explains an expired authorization code', () => {
    expect(
      oauthCallbackErrorText('TOKEN_EXCHANGE_FAILED', 'AUTH_CODE_INVALID_OR_EXPIRED'),
    ).toContain('授权码已过期或已被使用')
  })

  it('falls back to a stable user-facing message', () => {
    expect(oauthCallbackErrorText('UNEXPECTED_ERROR', '')).toContain('授权未完成')
  })

  it('explains that seller authorization cannot publish creator videos', () => {
    expect(oauthCallbackErrorText('CREATOR_TOKEN_REQUIRED', 'CREATOR_TOKEN_REQUIRED'))
      .toContain('必须使用达人授权')
  })
})


describe('authorizationActionLabel', () => {
  it('distinguishes initial connection from reauthorization', () => {
    expect(authorizationActionLabel('seller', false)).toBe('连接卖家账号')
    expect(authorizationActionLabel('seller', true)).toBe('重新授权卖家')
    expect(authorizationActionLabel('creator', false)).toBe('授权达人账号')
    expect(authorizationActionLabel('creator', true)).toBe('重新授权达人')
  })
})
