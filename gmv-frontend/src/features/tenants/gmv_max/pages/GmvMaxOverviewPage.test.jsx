import { describe, expect, it } from 'vitest';

import { formatOverviewRangeLabel } from './GmvMaxOverviewPage.jsx';

describe('formatOverviewRangeLabel timezone provenance', () => {
  it('identifies an official advertiser timezone when metadata provides it', () => {
    expect(
      formatOverviewRangeLabel(
        '7d',
        { start_date: '2026-07-11', end_date: '2026-07-17' },
        'America/Los_Angeles',
        true,
      ),
    ).toBe(
      '按广告主时区 America/Los_Angeles：近7天 2026-07-11 至 2026-07-17',
    );
  });

  it('identifies a browser fallback instead of presenting it as advertiser metadata', () => {
    expect(
      formatOverviewRangeLabel(
        'today',
        { start_date: '2026-07-17', end_date: '2026-07-17' },
        'Asia/Shanghai',
        false,
      ),
    ).toBe(
      '广告主时区未知，暂按浏览器时区 Asia/Shanghai：当天 2026-07-17',
    );
  });

  it('does not render a label for an incomplete date range', () => {
    expect(
      formatOverviewRangeLabel(
        'custom',
        { start_date: '2026-07-17', end_date: '' },
        'UTC',
        false,
      ),
    ).toBe('');
  });
});
