import { describe, expect, it } from 'vitest';

import {
  buildAdvertiserOptions,
  buildBusinessCenterOptions,
  buildStoreOptions,
} from './options.js';

describe('buildBusinessCenterOptions', () => {
  it('returns original list when selection is present', () => {
    const payload = { bcs: [{ bc_id: 'BC1', name: 'Center 1' }] };
    const result = buildBusinessCenterOptions(payload, 'BC1');
    expect(result).toHaveLength(1);
    expect(result[0].bc_id).toBe('BC1');
  });

  it('appends selected bc when missing', () => {
    const payload = { bcs: [] };
    const result = buildBusinessCenterOptions(payload, 'BC2');
    expect(result).toHaveLength(1);
    expect(result[0].bc_id).toBe('BC2');
  });
});

describe('buildAdvertiserOptions', () => {
  const advertisers = [
    { advertiser_id: 'ADV1', bc_id: 'BC1', name: 'Advertiser 1' },
    { advertiser_id: 'ADV2', bc_id: 'BC2', name: 'Advertiser 2' },
  ];

  it('returns all advertisers when bc is not selected', () => {
    const payload = { advertisers };
    const result = buildAdvertiserOptions(payload, '', '');
    expect(result.map((item) => item.advertiser_id)).toEqual(['ADV1', 'ADV2']);
  });

  it('filters advertisers by link mapping when available', () => {
    const payload = {
      advertisers,
      links: { bc_to_advertisers: { BC1: ['ADV1'] } },
    };
    const result = buildAdvertiserOptions(payload, 'BC1', '');
    expect(result).toHaveLength(1);
    expect(result[0].advertiser_id).toBe('ADV1');
  });

  it('falls back to bc_id matching when links are empty', () => {
    const payload = { advertisers };
    const result = buildAdvertiserOptions(payload, 'BC2', '');
    expect(result).toHaveLength(1);
    expect(result[0].advertiser_id).toBe('ADV2');
  });

  it('preserves selected advertiser when missing from list', () => {
    const payload = { advertisers };
    const result = buildAdvertiserOptions(payload, 'BC1', 'ADV3');
    expect(result.some((item) => item.advertiser_id === 'ADV3')).toBe(true);
  });
});

describe('buildStoreOptions', () => {
  const stores = [
    { store_id: 'STORE1', advertiser_id: 'ADV1', name: 'Store 1' },
    { store_id: 'STORE2', advertiser_id: 'ADV2', name: 'Store 2' },
  ];

  it('returns all stores when advertiser is not selected', () => {
    const payload = { stores };
    const result = buildStoreOptions(payload, '', '');
    expect(result.map((item) => item.store_id)).toEqual(['STORE1', 'STORE2']);
  });

  it('filters stores by mapping when available', () => {
    const payload = {
      stores,
      links: { advertiser_to_stores: { ADV1: ['STORE1'] } },
    };
    const result = buildStoreOptions(payload, 'ADV1', '');
    expect(result).toHaveLength(1);
    expect(result[0].store_id).toBe('STORE1');
  });

  it('keeps selected store when missing from list', () => {
    const payload = { stores };
    const result = buildStoreOptions(payload, 'ADV1', 'STORE3');
    expect(result.some((item) => item.store_id === 'STORE3')).toBe(true);
  });
});
