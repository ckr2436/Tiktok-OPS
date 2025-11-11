import { describe, expect, test } from 'vitest';
import * as api from './service.js';

describe('gmv_max minimal gmvmax api', () => {
  test('exports minimal campaign/strategy helpers', () => {
    expect(api.fetchGmvMaxCampaigns).toBeInstanceOf(Function);
    expect(api.fetchGmvMaxCampaignDetail).toBeInstanceOf(Function);
    expect(api.fetchGmvMaxStrategy).toBeInstanceOf(Function);
    expect(api.updateGmvMaxStrategy).toBeInstanceOf(Function);
    expect(api.previewGmvMaxStrategy).toBeInstanceOf(Function);
  });

  test('exports metrics & action helpers', () => {
    expect(api.fetchGmvMaxMetrics).toBeInstanceOf(Function);
    expect(api.applyGmvMaxAction).toBeInstanceOf(Function);
  });

  test('exports actions fetch helper', () => {
    expect(api.fetchGmvMaxActions).toBeInstanceOf(Function);
  });
});
