import { describe, expect, test } from 'vitest';
import * as api from '../ttb/gmvmax/api.js';

describe('ttb gmvmax api exports', () => {
  test('exports campaign & strategy helpers', () => {
    expect(api.listCampaigns).toBeInstanceOf(Function);
    expect(api.getCampaign).toBeInstanceOf(Function);
    expect(api.getStrategy).toBeInstanceOf(Function);
    expect(api.updateStrategy).toBeInstanceOf(Function);
    expect(api.previewStrategy).toBeInstanceOf(Function);
  });

  test('exports sync & metrics helpers', () => {
    expect(api.syncCampaigns).toBeInstanceOf(Function);
    expect(api.syncMetrics).toBeInstanceOf(Function);
    expect(api.queryMetrics).toBeInstanceOf(Function);
  });

  test('exports action helpers', () => {
    expect(api.listActionLogs).toBeInstanceOf(Function);
    expect(api.applyAction).toBeInstanceOf(Function);
  });
});
