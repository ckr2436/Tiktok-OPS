import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/http.js', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
  },
}));

import http from '@/lib/http.js';
import {
  listAllWebsiteAdsActions,
  listAllWebsiteAdsCampaigns,
  listAllWebsiteAdsDailyReports,
  listAllWebsiteAdsMediaPlans,
  listAllWebsiteAdsVideoUploads,
} from './api.js';


describe('Website Ads campaign pagination', () => {
  beforeEach(() => {
    http.get.mockReset();
  });

  it('loads every backend items/page/page_size/total page', async () => {
    const campaigns = Array.from({ length: 205 }, (_, index) => ({
      id: index + 1,
      campaign_id: `campaign-${index + 1}`,
    }));
    http.get.mockImplementation((_url, config) => {
      const { page, page_size: pageSize } = config.params;
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          items: campaigns.slice(start, start + pageSize),
          page,
          page_size: pageSize,
          total: campaigns.length,
        },
      });
    });

    const result = await listAllWebsiteAdsCampaigns(
      1,
      'tiktok-business',
      2,
      { page_size: 100, start_date: '2026-07-01', end_date: '2026-07-17' },
    );

    expect(http.get).toHaveBeenCalledTimes(3);
    expect(http.get.mock.calls.map(([, config]) => config.params.page)).toEqual([1, 2, 3]);
    expect(http.get.mock.calls.every(([, config]) => config.params.page_size === 100)).toBe(true);
    expect(result.items).toEqual(campaigns);
    expect(result).toMatchObject({ page: 1, page_size: 100, total: 205 });
  });

  it('rejects an empty non-terminal page instead of showing a partial campaign list', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [],
        page: 1,
        page_size: 100,
        total: 101,
      },
    });

    await expect(
      listAllWebsiteAdsCampaigns(1, 'tiktok-business', 2, { page_size: 100 }),
    ).rejects.toThrow('metadata requires 100');
  });

  it('rejects a repeated or unexpected response page', async () => {
    const repeatedItems = [{ id: 1, campaign_id: 'campaign-1' }];
    http.get
      .mockResolvedValueOnce({
        data: {
          items: repeatedItems,
          page: 1,
          page_size: 1,
          total: 2,
        },
      })
      .mockResolvedValueOnce({
        data: {
          items: repeatedItems,
          page: 2,
          page_size: 1,
          total: 2,
        },
      });

    await expect(
      listAllWebsiteAdsCampaigns(1, 'tiktok-business', 2, { page_size: 1 }),
    ).rejects.toThrow('repeated page content on page 2');

    http.get.mockReset();
    http.get.mockResolvedValue({
      data: {
        items: [{ id: 1 }],
        page: 1,
        page_size: 1,
        total: 2,
      },
    });
    await expect(
      listAllWebsiteAdsCampaigns(1, 'tiktok-business', 2, { page_size: 1 }),
    ).rejects.toThrow('returned page 1 for requested page 2');
  });

  it('rejects partially overlapping campaign pages with the expected raw row count', async () => {
    const pages = {
      1: [
        { id: 1, campaign_id: 'campaign-1' },
        { id: 2, campaign_id: 'campaign-2' },
      ],
      2: [
        { id: 2, campaign_id: 'campaign-2' },
        { id: 3, campaign_id: 'campaign-3' },
      ],
    };
    http.get.mockImplementation((_url, config) => {
      const { page } = config.params;
      return Promise.resolve({
        data: {
          items: pages[page],
          page,
          page_size: 2,
          total: 4,
        },
      });
    });

    await expect(
      listAllWebsiteAdsCampaigns(1, 'tiktok-business', 2, { page_size: 2 }),
    ).rejects.toMatchObject({
      code: 'WEBSITE_ADS_PAGINATION_INVARIANT',
      page: 2,
      reason: 'duplicate_item_key',
      duplicateScope: 'across_pages',
    });
    expect(http.get).toHaveBeenCalledTimes(2);
  });

  it('rejects missing stable ids instead of treating rows as a complete snapshot', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [{ campaign_id: 'campaign-without-local-id' }],
        page: 1,
        page_size: 1,
        total: 1,
      },
    });

    await expect(
      listAllWebsiteAdsCampaigns(1, 'tiktok-business', 2, { page_size: 1 }),
    ).rejects.toMatchObject({
      code: 'WEBSITE_ADS_PAGINATION_INVARIANT',
      page: 1,
      field: 'id',
      reason: 'missing_item_key',
    });
  });

  it('accepts an authoritative total=0 snapshot', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [],
        page: 1,
        page_size: 100,
        total: 0,
      },
    });

    const result = await listAllWebsiteAdsCampaigns(
      1,
      'tiktok-business',
      2,
      { page_size: 100 },
    );

    expect(result).toEqual({
      items: [],
      page: 1,
      page_size: 100,
      total: 0,
    });
    expect(http.get).toHaveBeenCalledTimes(1);
  });

  it('rejects a fetch-all response without an official total', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [{ id: 1, campaign_id: 'campaign-1' }],
        page: 1,
        page_size: 1,
      },
    });

    await expect(
      listAllWebsiteAdsCampaigns(1, 'tiktok-business', 2, { page_size: 1 }),
    ).rejects.toMatchObject({
      code: 'WEBSITE_ADS_PAGINATION_INVARIANT',
      page: 1,
      field: 'total',
    });
  });

  it('rejects a total that changes between pages', async () => {
    http.get
      .mockResolvedValueOnce({
        data: {
          items: [{ id: 1, campaign_id: 'campaign-1' }],
          page: 1,
          page_size: 1,
          total: 2,
        },
      })
      .mockResolvedValueOnce({
        data: {
          items: [{ id: 2, campaign_id: 'campaign-2' }],
          page: 2,
          page_size: 1,
          total: 3,
        },
      });

    await expect(
      listAllWebsiteAdsCampaigns(1, 'tiktok-business', 2, { page_size: 1 }),
    ).rejects.toMatchObject({
      code: 'WEBSITE_ADS_PAGINATION_INVARIANT',
      page: 2,
      field: 'total',
      expectedTotal: 2,
      receivedTotal: 3,
    });
  });

  it('raises explicitly when the configured page cap is reached', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [{ id: 1 }],
        page: 1,
        page_size: 1,
        total: 2,
      },
    });

    await expect(
      listAllWebsiteAdsCampaigns(
        1,
        'tiktok-business',
        2,
        { page_size: 1 },
        { maxPages: 1 },
      ),
    ).rejects.toThrow('pagination limit of 1 pages');
  });

  it('loads the second action-log page and never sends the legacy fixed limit', async () => {
    const actions = Array.from({ length: 205 }, (_, index) => ({
      id: index + 1,
      action: 'PAUSE_AD',
    }));
    http.get.mockImplementation((_url, config) => {
      const { page, page_size: pageSize } = config.params;
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          items: actions.slice(start, start + pageSize),
          page,
          page_size: pageSize,
          total: actions.length,
        },
      });
    });

    const result = await listAllWebsiteAdsActions(
      1,
      'tiktok-business',
      2,
      { page_size: 200, limit: 50 },
    );

    expect(http.get).toHaveBeenCalledTimes(2);
    expect(http.get.mock.calls.map(([, config]) => config.params.page)).toEqual([1, 2]);
    expect(http.get.mock.calls.every(([, config]) => config.params.limit === undefined)).toBe(true);
    expect(result.items).toEqual(actions);
    expect(result.total).toBe(205);
  });

  it('rejects duplicate action ids within a page even when row bodies differ', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [
          { id: 10, action: 'PAUSE_AD' },
          { id: 10, action: 'ENABLE_AD' },
        ],
        page: 1,
        page_size: 2,
        total: 2,
      },
    });

    await expect(
      listAllWebsiteAdsActions(1, 'tiktok-business', 2, { page_size: 2 }),
    ).rejects.toMatchObject({
      code: 'WEBSITE_ADS_PAGINATION_INVARIANT',
      page: 1,
      reason: 'duplicate_item_key',
      duplicateScope: 'within_page',
    });
  });

  it('loads every media-plan page and exposes the complete collection', async () => {
    const plans = Array.from({ length: 205 }, (_, index) => ({
      id: index + 1,
      name: `plan-${index + 1}`,
    }));
    http.get.mockImplementation((_url, config) => {
      const { page, page_size: pageSize } = config.params;
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          items: plans.slice(start, start + pageSize),
          page,
          page_size: pageSize,
          total: plans.length,
        },
      });
    });

    const result = await listAllWebsiteAdsMediaPlans(
      1,
      'tiktok-business',
      2,
      { page_size: 100, limit: 20 },
    );

    expect(http.get).toHaveBeenCalledTimes(3);
    expect(http.get.mock.calls.map(([, config]) => config.params.page)).toEqual([1, 2, 3]);
    expect(http.get.mock.calls.every(([, config]) => config.params.limit === undefined)).toBe(true);
    expect(result.items).toEqual(plans);
    expect(result.total).toBe(205);
  });

  it('loads every Website Ads daily-report page without a fixed-limit truncation', async () => {
    const reports = Array.from({ length: 401 }, (_, index) => ({
      id: index + 1,
      report_date: `2026-01-${String((index % 28) + 1).padStart(2, '0')}`,
    }));
    http.get.mockImplementation((_url, config) => {
      const { page, page_size: pageSize } = config.params;
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          items: reports.slice(start, start + pageSize),
          page,
          page_size: pageSize,
          total: reports.length,
        },
      });
    });

    const result = await listAllWebsiteAdsDailyReports(
      1,
      'tiktok-business',
      2,
      { page_size: 200, limit: 30 },
    );

    expect(http.get).toHaveBeenCalledTimes(3);
    expect(result.items).toEqual(reports);
    expect(result).toMatchObject({ page: 1, page_size: 200, total: 401 });
    expect(http.get.mock.calls.every(([, config]) => config.params.limit === undefined)).toBe(true);
  });

  it('strictly aggregates all requested upload ids across numbered pages', async () => {
    const uploads = Array.from({ length: 125 }, (_, index) => ({
      id: index + 1,
      upload_status: 'UPLOADED',
    }));
    const uploadIds = uploads.map((item) => item.id);
    http.get.mockImplementation((_url, config) => {
      const { page, page_size: pageSize } = config.params;
      const start = (page - 1) * pageSize;
      return Promise.resolve({
        data: {
          items: uploads.slice(start, start + pageSize),
          page,
          page_size: pageSize,
          total: uploads.length,
        },
      });
    });

    const result = await listAllWebsiteAdsVideoUploads(
      1,
      'tiktok-business',
      2,
      uploadIds,
      { page_size: 100 },
    );

    expect(http.get).toHaveBeenCalledTimes(2);
    expect(result.items).toEqual(uploads);
    expect(
      http.get.mock.calls.every(([, config]) => config.params.upload_ids === uploadIds.join(',')),
    ).toBe(true);
  });

  it('rejects an upload row without its stable upload id', async () => {
    http.get.mockResolvedValue({
      data: {
        items: [{ upload_status: 'UPLOADED' }],
        page: 1,
        page_size: 1,
        total: 1,
      },
    });

    await expect(
      listAllWebsiteAdsVideoUploads(
        1,
        'tiktok-business',
        2,
        [123],
        { page_size: 1 },
      ),
    ).rejects.toMatchObject({
      code: 'WEBSITE_ADS_PAGINATION_INVARIANT',
      page: 1,
      field: 'id',
      reason: 'missing_item_key',
    });
  });
});
