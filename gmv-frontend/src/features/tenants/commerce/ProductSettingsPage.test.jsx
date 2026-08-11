import { describe, expect, it } from 'vitest';

import { buildFlashSalePlan } from './ProductSettingsPage.jsx';


describe('buildFlashSalePlan', () => {
  it('keeps untouched products and applies staged enable and disable choices', () => {
    const products = [
      { product_id: 'product-1' },
      { product_id: 'product-2' },
      { product_id: 'product-3' },
    ];
    const policies = {
      'product-1': { enabled: true, activity_price_amount: '7.99' },
      'product-2': { enabled: true, activity_price_amount: '9.99' },
    };
    const drafts = {
      'product-2': { enabled: false, activity_price_amount: null },
      'product-3': { enabled: true, activity_price_amount: 12.99 },
    };

    expect(buildFlashSalePlan(products, policies, drafts)).toEqual([
      {
        product_id: 'product-1',
        enabled: true,
        activity_price_amount: 7.99,
      },
      {
        product_id: 'product-2',
        enabled: false,
        activity_price_amount: null,
      },
      {
        product_id: 'product-3',
        enabled: true,
        activity_price_amount: 12.99,
      },
    ]);
  });
});
