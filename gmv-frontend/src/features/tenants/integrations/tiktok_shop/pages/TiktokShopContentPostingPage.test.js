import { describe, expect, it } from 'vitest'

import {
  contentPostingStatusText,
  normalizeProductAnchorTitle,
} from './TiktokShopContentPostingPage.jsx'


describe('TikTok Shop content posting UI rules', () => {
  it('converts workflow states into clear Chinese labels', () => {
    expect(contentPostingStatusText('READY_TO_PUBLISH')).toBe('预检通过，待发布')
    expect(contentPostingStatusText('PUBLISH_UNCERTAIN')).toBe('发布结果待人工核对')
    expect(contentPostingStatusText('SUCCESS')).toBe('发布成功')
  })

  it('builds an official-compliant product anchor title', () => {
    expect(normalizeProductAnchorTitle('MYUPONA Sleep-Ease 😴 Gummies!')).toBe('MYUPONA SleepEase Gummies')
    expect(Array.from(normalizeProductAnchorTitle('很长的商品标题'.repeat(10))).length).toBe(30)
  })
})
