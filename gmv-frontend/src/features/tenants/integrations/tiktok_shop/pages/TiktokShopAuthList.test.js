import { describe, expect, it } from 'vitest'

import { oauthCallbackErrorText } from './TiktokShopAuthList.jsx'


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
})
