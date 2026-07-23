import { describe, expect, it } from 'vitest'

import {
  aggregateVideoRows,
  buildProductMatrix,
  buildDailyVideoSeries,
  dataTrustMeta,
  diagnoseVideos,
  filterAndSortVideos,
  formatVideoPublishedAt,
  nextDate,
  summarizeDiagnoses,
  summarizeOverviewDay,
  summarizeVideoAnalytics,
} from './videoAnalyticsUtils.js'

const videoRows = [
  {
    report_date: '2026-07-18',
    video_id: 'video-1',
    title: 'Earlier title',
    creator_username: 'creator-a',
    gmv: '10.00',
    views: 100,
    sku_orders: 1,
    items_sold: 1,
    click_through_rate: '0.10',
    products_json: [{ id: 'p1', name: 'Sleep Gummies' }],
    synced_at: '2026-07-19T01:00:00',
  },
  {
    report_date: '2026-07-19',
    video_id: 'video-1',
    title: 'Latest title',
    creator_username: 'creator-a',
    gmv: '15.00',
    views: 300,
    sku_orders: 2,
    items_sold: 2,
    click_through_rate: '0.20',
    products_json: [{ id: 'p2', name: 'Magnesium Gummies' }],
    synced_at: '2026-07-20T01:00:00',
  },
  {
    report_date: '2026-07-19',
    video_id: 'video-2',
    title: 'Second video',
    creator_username: 'creator-b',
    gmv: '5.00',
    views: 50,
    sku_orders: 1,
    click_through_rate: '0.05',
    products_json: [],
    synced_at: '2026-07-20T01:00:00',
  },
]

const overviewRows = [
  {
    report_date: '2026-07-18',
    gmv: '10.00',
    sku_orders: 1,
    product_impressions: 1000,
    product_clicks: 100,
    synced_at: '2026-07-20T02:00:00',
  },
  {
    report_date: '2026-07-19',
    gmv: '20.00',
    sku_orders: 3,
    product_impressions: 2000,
    product_clicks: 300,
    synced_at: '2026-07-20T02:00:00',
  },
]

describe('TikTok Shop video analytics helpers', () => {
  it('aggregates daily rows into one ranked row per video', () => {
    const videos = aggregateVideoRows(videoRows)
    const first = videos.find((item) => item.video_id === 'video-1')
    expect(videos).toHaveLength(2)
    expect(first).toMatchObject({
      title: 'Latest title',
      gmv: 25,
      views: 400,
      sku_orders: 3,
      estimated_product_clicks: 70,
      report_days: 2,
    })
    expect(first.products.map((item) => item.id)).toEqual(['p1', 'p2'])
    expect(first.click_through_rate).toBeCloseTo(0.175)
  })

  it('builds daily trend rows without double counting overview GMV', () => {
    expect(buildDailyVideoSeries(videoRows, overviewRows)).toEqual([
      expect.objectContaining({ date: '2026-07-18', gmv: 10, views: 100, orders: 1, videos: 1 }),
      expect.objectContaining({ date: '2026-07-19', gmv: 20, views: 350, orders: 3, videos: 2 }),
    ])
  })

  it('summarizes official overview and video metrics', () => {
    expect(summarizeVideoAnalytics(videoRows, overviewRows)).toMatchObject({
      gmv: 30,
      views: 450,
      orders: 4,
      impressions: 3000,
      clicks: 400,
      ctr: 400 / 450,
      product_impression_click_rate: 400 / 3000,
      latest_report_date: '2026-07-19',
    })
  })

  it('keeps official video CTR separate and exposes today/yesterday source state', () => {
    expect(summarizeOverviewDay([
      {
        report_date: '2026-07-21', gmv: '23.27', sku_orders: 2,
        product_impressions: 2000, product_clicks: 40,
        click_through_rate: null, data_source: 'shop_and_product_video_channels',
        is_provisional: true, synced_at: '2026-07-22T03:00:00',
      },
    ], '2026-07-21')).toMatchObject({
      available: true,
      gmv: 23.27,
      click_through_rate: null,
      data_source: 'shop_and_product_video_channels',
      is_provisional: true,
    })
    expect(summarizeOverviewDay([], '2026-07-20')).toMatchObject({
      available: false,
      report_date: '2026-07-20',
    })
  })

  it('filters by linked product and sorts by the selected metric', () => {
    const videos = aggregateVideoRows(videoRows)
    expect(filterAndSortVideos(videos, 'magnesium', 'views').map((item) => item.video_id))
      .toEqual(['video-1'])
  })

  it('converts the inclusive screen end date to the API exclusive end date', () => {
    expect(nextDate('2026-07-31')).toBe('2026-08-01')
  })

  it('formats video publish time in the selected shop timezone', () => {
    expect(formatVideoPublishedAt('2026-07-21T12:30:00', 'America/New_York'))
      .toMatch(/2026.*07.*21.*08.*30/)
    expect(formatVideoPublishedAt(null, 'America/New_York')).toBe('--')
  })

  it('creates transparent deterministic diagnoses with evidence', () => {
    const diagnosed = diagnoseVideos(videoRows)
    const first = diagnosed.find((item) => item.video_id === 'video-1')
    expect(first.diagnosis).toMatchObject({
      code: 'PRODUCT_HANDOFF',
      rule_version: 'shop-video-rules-v1',
      evidence: { report_days: 2, views: 400 },
    })
    expect(summarizeDiagnoses(diagnosed).counts.PRODUCT_HANDOFF).toBe(1)
  })

  it('builds a product matrix without assigning video GMV to linked products', () => {
    const diagnosed = diagnoseVideos(videoRows)
    const matrix = buildProductMatrix([
      {
        product_id: 'p1', gmv: '80', orders: 4, product_impressions: 1000,
        product_clicks: 100, refund_amount: '8', currency: 'USD',
      },
    ], [{ product_id: 'p1', title: 'Sleep Gummies', main_image_url: 'https://img.test/p1.jpg' }], diagnosed)
    expect(matrix.find((item) => item.product_id === 'p1')).toMatchObject({
      gmv: 80,
      linked_video_count: 1,
      click_through_rate: 0.1,
      refund_rate: 0.1,
    })
  })

  it('labels data freshness and pagination truthfully', () => {
    expect(dataTrustMeta({
      endDate: '2026-07-19', latestReportDate: '2026-07-19',
      today: '2026-07-19',
      syncedAt: '2026-07-20T02:00:00', rowCount: 3, total: 3,
    })).toMatchObject({
      complete: true,
      completeness_label: '完整分页',
      freshness_label: '当日可变数据',
    })
  })
})
