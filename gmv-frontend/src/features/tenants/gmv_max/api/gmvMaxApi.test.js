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
import { getGmvMaxMetrics } from './gmvMaxApi.js';
import { GmvMaxMetricsLevel } from '../constants/metrics.js';

describe('getGmvMaxMetrics', () => {
  beforeEach(() => {
    http.get.mockClear();
  });

  it('uses overview level from metrics enum', async () => {
    await getGmvMaxMetrics(2, 'tiktok-business', 3, 'all', {
      level: GmvMaxMetricsLevel.OVERVIEW,
      start_date: '2025-12-07',
      end_date: '2025-12-07',
      store_ids: ['store-1'],
      dimensions: ['stat_time_day'],
    });

    expect(http.get).toHaveBeenCalledTimes(1);
    const [, config] = http.get.mock.calls[0];
    expect(config.params.get('level')).toBe(GmvMaxMetricsLevel.OVERVIEW);
  });

  it('injects campaign id when creative level requires it', async () => {
    await getGmvMaxMetrics(1, 'tiktok-business', 2, 'cmp-123', {
      level: GmvMaxMetricsLevel.CREATIVE,
    });

    const [, config] = http.get.mock.calls[0];
    expect(config.params.getAll('campaign_ids')).toEqual(['cmp-123']);
  });
});
