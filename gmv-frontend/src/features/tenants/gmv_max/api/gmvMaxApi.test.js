import { describe, expect, it, vi, beforeEach } from 'vitest';

vi.mock('@/lib/http.js', () => {
  const get = vi.fn(() => Promise.resolve({ data: {} }));
  const post = vi.fn(() => Promise.resolve({ data: {} }));
  const put = vi.fn(() => Promise.resolve({ data: {} }));

  return {
    default: { get, post, put },
  };
});

import http from '@/lib/http.js';
import {
  createGmvMaxCampaign,
  getAccountSyncRun,
  getGmvMaxMetrics,
  listAccounts,
  listGmvMaxCampaigns,
  listGmvMaxCreativeAssets,
  listGmvMaxHermesDailyReports,
  listProducts,
  precheckGmvMaxCampaign,
  refreshGmvMaxCreativeAssets,
  startGmvMaxCreativeHeating,
  stopGmvMaxCreativeHeating,
  waitForAccountSyncRun,
} from './gmvMaxApi.js';
import { GmvMaxMetricsLevel } from '../constants/metrics.js';

describe('GMV Max create request timeout', () => {
  beforeEach(() => {
    http.post.mockReset();
    http.post.mockResolvedValue({ data: { campaign: { campaign_id: 'campaign-1' } } });
  });

  it('allows the official create chain to run longer than the global 15 second timeout', async () => {
    await createGmvMaxCampaign(
      1,
      'tiktok-business',
      2,
      { store_id: 'store-1' },
    );

    expect(http.post).toHaveBeenCalledWith(
      expect.stringContaining('/gmvmax'),
      { store_id: 'store-1' },
      { timeout: 120000 },
    );
  });

  it('applies the same timeout floor to precheck and preserves longer caller timeouts', async () => {
    await precheckGmvMaxCampaign(
      1,
      'tiktok-business',
      2,
      { store_id: 'store-1' },
      { timeout: 180000, signal: 'signal-token' },
    );

    expect(http.post).toHaveBeenCalledWith(
      expect.stringContaining('/gmvmax/precheck'),
      { store_id: 'store-1' },
      { timeout: 180000, signal: 'signal-token' },
    );
  });
});

describe('account foundation sync polling', () => {
  beforeEach(() => {
    http.get.mockReset();
  });

  it('keeps the run lookup inside the selected tenant and account route', async () => {
    http.get.mockResolvedValue({ data: { id: 77, status: 'success' } });

    await getAccountSyncRun(3, 'tiktok-business', 9, 77);

    expect(http.get).toHaveBeenCalledWith(
      '/tenants/3/providers/tiktok-business/accounts/9/sync-runs/77',
      undefined,
    );
  });

  it('waits for a terminal persisted run instead of treating 202 as completion', async () => {
    http.get
      .mockResolvedValueOnce({ data: { id: 77, status: 'running' } })
      .mockResolvedValueOnce({ data: { id: 77, status: 'success' } });

    const run = await waitForAccountSyncRun(3, 'tiktok-business', 9, 77, {
      pollIntervalMs: 0,
    });

    expect(run.status).toBe('success');
    expect(http.get).toHaveBeenCalledTimes(2);
  });

  it('returns a failed terminal run so the page can show its stage error', async () => {
    http.get.mockResolvedValue({
      data: {
        id: 77,
        status: 'failed',
        error_code: 'PAGINATION_ITEM_KEY_INVALID',
        error_message: 'official row key missing',
      },
    });

    const run = await waitForAccountSyncRun(3, 'tiktok-business', 9, 77, {
      pollIntervalMs: 0,
    });

    expect(run).toMatchObject({
      status: 'failed',
      error_code: 'PAGINATION_ITEM_KEY_INVALID',
    });
  });
});

describe('getGmvMaxMetrics', () => {
  beforeEach(() => {
    http.get.mockReset();
    http.get.mockResolvedValue({ data: {} });
  });

  it('uses overview level from metrics enum', async () => {
    await getGmvMaxMetrics(2, 'tiktok-business', 3, undefined, {
      level: GmvMaxMetricsLevel.OVERVIEW,
      start_date: '2025-12-07',
      end_date: '2025-12-07',
      store_id: 'store-1',
      fetch_all_pages: false,
    });

    expect(http.get).toHaveBeenCalledTimes(1);
    const [url, config] = http.get.mock.calls[0];
    expect(url).toContain('/gmvmax/metrics');
    expect(config.params.get('level')).toBe(GmvMaxMetricsLevel.OVERVIEW);
    expect(config.params.get('page_size')).toBe('1000');
  });

  it('injects campaign id when creative level requires it', async () => {
    await getGmvMaxMetrics(1, 'tiktok-business', 2, 'cmp-123', {
      level: GmvMaxMetricsLevel.CREATIVE,
      fetch_all_pages: false,
    });

    const [, config] = http.get.mock.calls[0];
    expect(config.params.getAll('campaign_ids')).toEqual(['cmp-123']);
  });

  it('preserves every product binding for a multi-product creative report', async () => {
    await getGmvMaxMetrics(1, 'tiktok-business', 2, 'cmp-123', {
      level: GmvMaxMetricsLevel.CREATIVE,
      item_group_ids: ['product-2', 'product-1'],
      fetch_all_pages: false,
    });

    const [, config] = http.get.mock.calls[0];
    expect(config.params.getAll('item_group_ids')).toEqual(['product-1', 'product-2']);
  });

  it('loads and merges every numbered report page without dropping repeated creative ids', async () => {
    const rows = Array.from({ length: 148 }, (_, index) => ({
      dimensions: {
        creative_id: index % 2 === 0 ? '-1' : `creative-${index % 10}`,
        item_group_id: `product-${index}`,
        stat_time_day: `2026-07-${String((index % 17) + 1).padStart(2, '0')}`,
      },
      metrics: { spend: index + 1 },
    }));
    const summary = { spend: 11175, gmv: 22350 };
    const freshness = { state: 'fresh', source: 'creative-daily' };
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.get('page'));
      const pageSize = Number(config.params.get('page_size'));
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          report: {
            list: rows.slice(start, start + pageSize),
            page_info: {
              page,
              page_size: pageSize,
              total_number: rows.length,
              total_page: 3,
              has_more: page < 3,
              has_next: page < 3,
            },
            summary,
          },
          freshness,
        },
      });
    });

    const result = await getGmvMaxMetrics(1, 'tiktok-business', 2, 'cmp-123', {
      level: GmvMaxMetricsLevel.CREATIVE,
      page_size: 50,
    });

    expect(http.get).toHaveBeenCalledTimes(3);
    expect(http.get.mock.calls.map(([, config]) => config.params.get('page'))).toEqual(['1', '2', '3']);
    expect(result.report.list).toEqual(rows);
    expect(result.report.list.filter((row) => row.dimensions.creative_id === '-1')).toHaveLength(74);
    expect(result.report.summary).toBe(summary);
    expect(result.freshness).toBe(freshness);
    expect(result.report.page_info).toMatchObject({
      page: 1,
      page_size: 148,
      total_number: 148,
      total_page: 1,
      has_more: false,
      has_next: false,
    });
  });

  it('rejects partially overlapping report pages by canonical dimensions', async () => {
    const pages = {
      1: [
        {
          dimensions: { campaign_id: 'campaign-1', stat_time_day: '2026-07-01' },
          metrics: { spend: 1 },
        },
        {
          dimensions: { campaign_id: 'campaign-2', stat_time_day: '2026-07-01' },
          metrics: { spend: 2 },
        },
      ],
      2: [
        {
          dimensions: { stat_time_day: '2026-07-01', campaign_id: 'campaign-2' },
          metrics: { spend: 3 },
        },
        {
          dimensions: { campaign_id: 'campaign-3', stat_time_day: '2026-07-01' },
          metrics: { spend: 4 },
        },
      ],
    };
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.get('page'));
      return Promise.resolve({
        data: {
          report: {
            list: pages[page],
            page_info: {
              page,
              page_size: 2,
              total_number: 4,
              total_page: 2,
              has_more: page < 2,
              has_next: page < 2,
            },
          },
        },
      });
    });

    await expect(
      getGmvMaxMetrics(1, 'tiktok-business', 2, 'campaign-1', {
        level: GmvMaxMetricsLevel.CAMPAIGN,
        page_size: 2,
      }),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      page: 2,
      reason: 'duplicate_item_key',
      duplicateScope: 'across_pages',
    });
    expect(http.get).toHaveBeenCalledTimes(2);
  });

  it('rejects a report row without a non-empty dimensions key', async () => {
    http.get.mockResolvedValue({
      data: {
        report: {
          list: [{ metrics: { spend: 1 } }],
          page_info: {
            page: 1,
            page_size: 1,
            total_number: 1,
            total_page: 1,
            has_more: false,
            has_next: false,
          },
        },
      },
    });

    await expect(
      getGmvMaxMetrics(1, 'tiktok-business', 2, 'campaign-1', {
        level: GmvMaxMetricsLevel.CAMPAIGN,
        page_size: 1,
      }),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      page: 1,
      field: 'report dimensions',
      reason: 'missing_item_key',
    });
  });

  it('aborts before requesting another page', async () => {
    const controller = new AbortController();
    http.get.mockImplementationOnce(() => {
      controller.abort();
      return Promise.resolve({
        data: {
          report: {
            list: Array.from({ length: 50 }, (_, index) => ({ id: index })),
            page_info: {
              page: 1,
              page_size: 50,
              total_number: 100,
              total_page: 2,
              has_next: true,
            },
          },
        },
      });
    });

    await expect(
      getGmvMaxMetrics(
        1,
        'tiktok-business',
        2,
        'cmp-123',
        { level: GmvMaxMetricsLevel.CREATIVE },
        { signal: controller.signal },
      ),
    ).rejects.toMatchObject({ name: 'AbortError' });
    expect(http.get).toHaveBeenCalledTimes(1);
    expect(http.get.mock.calls[0][1].signal).toBe(controller.signal);
  });

  it('throws instead of returning a partial report when the page limit is exceeded', async () => {
    http.get.mockResolvedValue({
      data: {
        report: {
          list: Array.from({ length: 50 }, (_, index) => ({ id: index })),
          page_info: {
            page: 1,
            page_size: 50,
            total_number: 150,
            total_page: 3,
            has_next: true,
          },
        },
      },
    });

    await expect(
      getGmvMaxMetrics(1, 'tiktok-business', 2, 'cmp-123', {
        level: GmvMaxMetricsLevel.CREATIVE,
        max_pages: 2,
      }),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_LIMIT_EXCEEDED',
      maxPages: 2,
      totalPages: 3,
    });
    expect(http.get).toHaveBeenCalledTimes(1);
    expect(http.get.mock.calls[0][1].params.has('max_pages')).toBe(false);
  });
});

describe('numbered collection pagination', () => {
  beforeEach(() => {
    http.get.mockReset();
    http.get.mockResolvedValue({ data: {} });
  });

  it('accepts an authoritative empty account collection', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [],
        page: 1,
        page_size: 100,
        total: 0,
      },
    });

    const result = await listAccounts(1, 'tiktok-business', { page_size: 100 });

    expect(result).toEqual({
      items: [],
      page: 1,
      page_size: 100,
      total: 0,
    });
    expect(http.get).toHaveBeenCalledTimes(1);
  });

  it('rejects an account row without auth_id', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [{ label: 'missing stable key' }],
        page: 1,
        page_size: 100,
        total: 1,
      },
    });

    await expect(
      listAccounts(1, 'tiktok-business', { page_size: 100 }),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      field: 'account auth_id',
      reason: 'missing_item_key',
    });
  });

  it('merges top-level total/page/page_size product responses', async () => {
    const products = Array.from({ length: 120 }, (_, index) => ({ product_id: `product-${index}` }));
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.page);
      const pageSize = Number(config.params.page_size);
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          items: products.slice(start, start + pageSize),
          page,
          page_size: pageSize,
          total: products.length,
        },
      });
    });

    const result = await listProducts(1, 'tiktok-business', 2, { store_id: 'store-1', page_size: 50 });

    expect(http.get).toHaveBeenCalledTimes(3);
    expect(result.items).toEqual(products);
    expect(result).toMatchObject({ page: 1, page_size: 120, total: 120 });
  });

  it('rejects a fetch-all product response without an authoritative total', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [{ product_id: 'product-1' }],
        page: 1,
        page_size: 50,
      },
    });

    await expect(
      listProducts(1, 'tiktok-business', 2, { store_id: 'store-1', page_size: 50 }),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      page: 1,
      field: 'total',
    });
  });

  it('merges item responses that expose page_info', async () => {
    const campaigns = Array.from({ length: 75 }, (_, index) => ({ campaign_id: `campaign-${index}` }));
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.page);
      const pageSize = Number(config.params.page_size);
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          items: campaigns.slice(start, start + pageSize),
          page_info: {
            page,
            page_size: pageSize,
            total_number: campaigns.length,
          },
        },
      });
    });

    const result = await listGmvMaxCampaigns(1, 'tiktok-business', 2, { page_size: 50 });

    expect(http.get).toHaveBeenCalledTimes(2);
    expect(result.items).toEqual(campaigns);
    expect(result.page_info).toMatchObject({
      page: 1,
      page_size: 75,
      total_number: 75,
      total_page: 1,
      has_next: false,
    });
  });

  it('rejects partially overlapping campaign pages even when raw row counts equal total', async () => {
    const pageItems = {
      1: [
        { campaign_id: 'campaign-1' },
        { campaign_id: 'campaign-2' },
      ],
      2: [
        { campaign_id: 'campaign-2' },
        { campaign_id: 'campaign-3' },
      ],
    };
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.page);
      return Promise.resolve({
        data: {
          items: pageItems[page],
          page_info: {
            page,
            page_size: 2,
            total_number: 4,
            total_page: 2,
            has_more: page < 2,
            has_next: page < 2,
          },
        },
      });
    });

    await expect(
      listGmvMaxCampaigns(1, 'tiktok-business', 2, { page_size: 2 }),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      page: 2,
      reason: 'duplicate_item_key',
      duplicateScope: 'across_pages',
    });
    expect(http.get).toHaveBeenCalledTimes(2);
  });

  it('fails closed when continuation flags contradict the official total', async () => {
    const campaigns = Array.from({ length: 3 }, (_, index) => ({ campaign_id: `campaign-${index}` }));
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.page);
      const pageSize = Number(config.params.page_size);
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          items: campaigns.slice(start, start + pageSize),
          page_info: {
            page,
            page_size: pageSize,
            total_number: campaigns.length,
            total_page: 2,
            has_more: false,
            has_next: false,
          },
        },
      });
    });

    await expect(
      listGmvMaxCampaigns(1, 'tiktok-business', 2, { page_size: 2 }),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      field: 'has_next',
      page: 1,
      expectedHasNext: true,
      receivedHasNext: false,
    });
    expect(http.get).toHaveBeenCalledTimes(1);
  });

  it('throws an invariant error when an empty page claims another page exists', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [],
        page_info: {
          page: 1,
          page_size: 50,
          total_number: 100,
          total_page: 2,
          has_more: false,
          has_next: false,
        },
      },
    });

    await expect(
      listGmvMaxCampaigns(1, 'tiktok-business', 2, { page_size: 50 }),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      page: 1,
    });
    expect(http.get).toHaveBeenCalledTimes(1);
  });

  it('only auto-pages creative assets when fetch_all_pages is explicit', async () => {
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.page || 1);
      return Promise.resolve({
        data: {
          items: [{ item_id: `creative-${page}` }],
          page_info: {
            page,
            page_size: 1,
            total_number: 2,
            total_page: 2,
            has_next: page < 2,
          },
        },
      });
    });

    const firstPage = await listGmvMaxCreativeAssets(
      1,
      'tiktok-business',
      2,
      { store_id: 'store-1', page_size: 1 },
    );
    expect(firstPage.items).toEqual([{ item_id: 'creative-1' }]);
    expect(http.get).toHaveBeenCalledTimes(1);

    http.get.mockClear();
    const allPages = await listGmvMaxCreativeAssets(
      1,
      'tiktok-business',
      2,
      { store_id: 'store-1', page_size: 1, fetch_all_pages: true },
    );
    expect(http.get).toHaveBeenCalledTimes(2);
    expect(allPages.items).toEqual([{ item_id: 'creative-1' }, { item_id: 'creative-2' }]);
    expect(http.get.mock.calls[0][1].params).not.toHaveProperty('fetch_all_pages');
    expect(http.get.mock.calls[0][1].paramsSerializer).toEqual({ indexes: null });
  });

  it('fails fast when an endpoint ignores the requested next page', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [{ item_id: 'creative-1' }],
        page_info: {
          page: 1,
          page_size: 1,
          total_number: 2,
          total_page: 2,
          has_next: true,
        },
      },
    });

    await expect(
      listGmvMaxCreativeAssets(
        1,
        'tiktok-business',
        2,
        { store_id: 'store-1', page_size: 1, fetch_all_pages: true },
      ),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_STALLED',
      requestedPage: 2,
      receivedPage: 1,
    });
    expect(http.get).toHaveBeenCalledTimes(2);
  });

  it('fails fast when different page numbers repeat the same page content', async () => {
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.page);
      return Promise.resolve({
        data: {
          items: [{ item_id: 'repeated-creative' }],
          page_info: {
            page,
            page_size: 1,
            total_number: 2,
            total_page: 2,
            has_next: page < 2,
          },
        },
      });
    });

    await expect(
      listGmvMaxCreativeAssets(
        1,
        'tiktok-business',
        2,
        { store_id: 'store-1', page_size: 1, fetch_all_pages: true },
      ),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_STALLED',
      requestedPage: 2,
      receivedPage: 2,
      reason: 'repeated_page_content',
    });
    expect(http.get).toHaveBeenCalledTimes(2);
  });

  it('rejects duplicate creative item_id values within one page', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [
          { item_id: 'creative-1', title: 'first' },
          { item_id: 'creative-1', title: 'changed duplicate' },
        ],
        page_info: {
          page: 1,
          page_size: 2,
          total_number: 2,
          total_page: 1,
          has_more: false,
          has_next: false,
        },
      },
    });

    await expect(
      listGmvMaxCreativeAssets(
        1,
        'tiktok-business',
        2,
        { store_id: 'store-1', page_size: 2, fetch_all_pages: true },
      ),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      page: 1,
      field: 'creative item_id',
      reason: 'duplicate_item_key',
      duplicateScope: 'within_page',
    });
  });

  it('strictly aggregates every Hermes ad daily-report page', async () => {
    const reports = Array.from({ length: 125 }, (_, index) => ({
      id: index + 1,
      report_date: `2026-07-${String((index % 28) + 1).padStart(2, '0')}`,
    }));
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.page);
      const pageSize = Number(config.params.page_size);
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          list: reports.slice(start, start + pageSize),
          page,
          page_size: pageSize,
          total: reports.length,
          latest: reports[0],
        },
      });
    });

    const result = await listGmvMaxHermesDailyReports(
      1,
      'tiktok-business',
      2,
      { page_size: 60, limit: 14, fetch_all_pages: true },
    );

    expect(http.get).toHaveBeenCalledTimes(3);
    expect(result.list).toEqual(reports);
    expect(result).toMatchObject({ page: 1, page_size: 125, total: 125 });
    expect(http.get.mock.calls.every(([, config]) => config.params.limit === undefined)).toBe(true);
  });

  it('rejects a changing total during strict local-list aggregation', async () => {
    http.get.mockImplementation((_url, config) => {
      const page = Number(config.params.page);
      return Promise.resolve({
        data: {
          list: [{ id: page }],
          page,
          page_size: 1,
          total: page === 1 ? 2 : 3,
        },
      });
    });

    await expect(
      listGmvMaxHermesDailyReports(
        1,
        'tiktok-business',
        2,
        { page_size: 1, fetch_all_pages: true },
      ),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      expectedTotal: 2,
      receivedTotal: 3,
    });
  });

  it('rejects a Hermes report without its stable database id', async () => {
    http.get.mockResolvedValue({
      data: {
        list: [{ report_date: '2026-07-17' }],
        page: 1,
        page_size: 1,
        total: 1,
      },
    });

    await expect(
      listGmvMaxHermesDailyReports(
        1,
        'tiktok-business',
        2,
        { page_size: 1, fetch_all_pages: true },
      ),
    ).rejects.toMatchObject({
      code: 'GMVMAX_PAGINATION_INVARIANT',
      page: 1,
      field: 'Hermes report id',
      reason: 'missing_item_key',
    });
  });

  it('uses the admin-only POST endpoint for an explicit creative refresh', async () => {
    http.post.mockResolvedValueOnce({ data: { sync: { upserted: 148 } } });

    const result = await refreshGmvMaxCreativeAssets(
      1,
      'tiktok-business',
      2,
      {
        store_id: 'store-1',
        advertiser_id: 'advertiser-1',
        item_group_id: 'product-1',
      },
    );

    expect(result).toEqual({ sync: { upserted: 148 } });
    expect(http.post).toHaveBeenCalledWith(
      expect.stringContaining('/gmvmax/creative-assets/refresh'),
      {},
      {
        params: {
          store_id: 'store-1',
          advertiser_id: 'advertiser-1',
          item_group_id: 'product-1',
        },
      },
    );
  });
});

describe('creative heating action payloads', () => {
  beforeEach(() => {
    http.post.mockClear();
  });

  it('sends a BOOST_CREATIVE body accepted by CreativeHeatingActionRequest', async () => {
    await startGmvMaxCreativeHeating(
      1,
      'tiktok-business',
      2,
      'campaign-1',
      'creative-1',
      {
        mode: 'MANUAL',
        target_daily_budget: 25,
        currency: 'USD',
        max_duration_minutes: 60,
        note: 'manual safety test',
      },
    );

    expect(http.post).toHaveBeenCalledTimes(1);
    const [url, body] = http.post.mock.calls[0];
    expect(url).toContain('/gmvmax/campaign-1/actions');
    expect(body).toEqual({
      action_type: 'BOOST_CREATIVE',
      creative_id: 'creative-1',
      mode: 'MANUAL',
      target_daily_budget: 25,
      currency: 'USD',
      max_duration_minutes: 60,
      note: 'manual safety test',
    });
  });

  it('sends STOP as the canonical BOOST_CREATIVE stop mode', async () => {
    await stopGmvMaxCreativeHeating(
      1,
      'tiktok-business',
      2,
      'campaign-1',
      'creative-1',
      {
        product_id: 'product-1',
        item_id: 'creative-1',
      },
    );

    expect(http.post).toHaveBeenCalledTimes(1);
    const [, body] = http.post.mock.calls[0];
    expect(body).toEqual({
      action_type: 'BOOST_CREATIVE',
      creative_id: 'creative-1',
      mode: 'STOP',
      product_id: 'product-1',
      item_id: 'creative-1',
    });
  });
});
