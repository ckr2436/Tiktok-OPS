import {
  advertisersForShop,
  dateInTimezone,
  rangeForPreset,
  rangeLabel,
} from './commerceUtils.js';

describe('commerceUtils', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('builds presets from the advertiser timezone', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-20T03:30:00Z'));

    expect(rangeForPreset('today', 'America/New_York')).toEqual({
      start_date: '2026-07-19',
      end_date: '2026-07-19',
    });
    expect(rangeForPreset('today', 'Asia/Shanghai')).toEqual({
      start_date: '2026-07-20',
      end_date: '2026-07-20',
    });
    expect(rangeForPreset('7d', 'America/New_York')).toEqual({
      start_date: '2026-07-13',
      end_date: '2026-07-19',
    });
  });

  it('formats dates in the selected IANA timezone', () => {
    const value = new Date('2026-01-01T05:00:00Z');

    expect(dateInTimezone(value, 'America/New_York')).toBe('2026-01-01');
    expect(dateInTimezone(value, 'Etc/GMT+8')).toBe('2025-12-31');
    expect(rangeLabel('2026-07-01', '2026-07-08')).toBe(
      '2026-07-01 至 2026-07-08',
    );
  });

  it('returns only advertisers belonging to the selected shop', () => {
    const context = {
      shops: [
        { id: 1, advertisers: [{ advertiser_id: 'a' }] },
        { id: 2, advertisers: [{ advertiser_id: 'b' }] },
      ],
    };

    expect(advertisersForShop(context, '2')).toEqual([
      { advertiser_id: 'b' },
    ]);
    expect(advertisersForShop(context, 'missing')).toEqual([]);
  });
});
