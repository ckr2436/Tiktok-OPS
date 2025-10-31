import { describe, expect, it } from 'vitest';

import {
  deriveShopBcDisplay,
  extractProductCurrency,
  extractProductTitle,
  formatPrice,
  formatStock,
  safeText,
} from './accountOverview.js';

describe('account overview utils', () => {
  it('derives bc id from direct column', () => {
    const result = deriveShopBcDisplay({ bc_id: '123', raw: { store_authorized_bc_id: '456' } });
    expect(result).toEqual({ value: '123', needsBackfill: false });
  });

  it('falls back to raw store_authorized_bc_id when missing bc_id', () => {
    const result = deriveShopBcDisplay({ raw: { store_authorized_bc_id: '789' } });
    expect(result.value).toBe('789');
    expect(result.needsBackfill).toBe(true);
  });

  it('supports raw_json fallback for bc id', () => {
    const result = deriveShopBcDisplay({ raw_json: { store_authorized_bc_id: '555' } });
    expect(result.value).toBe('555');
    expect(result.needsBackfill).toBe(true);
  });

  it('formats price with currency and precision', () => {
    expect(formatPrice({ price: 12, currency: 'USD' })).toBe('USD 12.00');
  });

  it('formats price from raw object', () => {
    const product = { raw: { price: { amount: '9.5', currency: 'EUR' } } };
    expect(formatPrice(product)).toBe('EUR 9.50');
  });

  it('returns placeholder when price missing', () => {
    expect(formatPrice({})).toBe('-');
  });

  it('formats stock with numeric values', () => {
    expect(formatStock({ stock: 5 })).toBe('5');
    expect(formatStock({ stock: { quantity: 3 } })).toBe('3');
  });

  it('formats stock from raw json fallback', () => {
    expect(formatStock({ raw_json: { stock: { available: 8 } } })).toBe('8');
  });

  it('extracts product title with raw fallback', () => {
    expect(extractProductTitle({ raw_json: { title: 'Raw Title' } })).toBe('Raw Title');
  });

  it('extracts product currency with raw price fallback', () => {
    expect(extractProductCurrency({ raw: { price: { currency: 'SGD' } } })).toBe('SGD');
  });

  it('returns fallback for empty text', () => {
    expect(safeText('')).toBe('-');
    expect(safeText('value')).toBe('value');
  });
});
