// src/features/platform/gmvmax/constants.js

export const MONITORING_STRATEGY_LEVELS = [
  'OVERVIEW_DAILY',
  'CAMPAIGN_DAILY',
  'CAMPAIGN_HOURLY',
  'PRODUCT_DAILY',
  'PRODUCT_HOURLY',
  'CREATIVE_10MIN',
  'LIVESTREAM_DAILY',
  'LIVESTREAM_HOURLY',
  'DURATION_DAILY',
  'DURATION_HOURLY',
]

export const PROMOTION_TYPES = ['PRODUCT', 'LIVE']

export const STRATEGY_CATEGORIES = [
  { value: 'GMVMAX', label: 'GMV Max 指标策略' },
  { value: 'TTB_BASE', label: 'TikTok 基础数据同步' },
]

export const TASK_OPTIONS_BY_CATEGORY = {
  GMVMAX: [
    { value: 'gmvmax.strategy', label: 'GMV Max 指标策略（按 level 决定粒度）' },
  ],
  TTB_BASE: [
    { value: 'ttb.sync.products', label: '商品增量同步 (ttb.sync.products)' },
    { value: 'ttb.sync.stores', label: '门店增量同步 (ttb.sync.stores)' },
    { value: 'ttb.sync.advertisers', label: '广告主增量同步 (ttb.sync.advertisers)' },
    { value: 'ttb.sync.meta', label: '元数据刷新 (ttb.sync.meta)' },
    { value: 'ttb.sync.bc', label: 'Business Center 同步 (ttb.sync.bc)' },
  ],
}

export function getCategoryLabel(value) {
  return STRATEGY_CATEGORIES.find((opt) => opt.value === value)?.label || value || '-'
}

export function getTaskLabel(category, taskName) {
  const opts = TASK_OPTIONS_BY_CATEGORY[category] || []
  return opts.find((opt) => opt.value === taskName)?.label || taskName || '-'
}
