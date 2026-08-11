import { beforeEach, describe, expect, it } from 'vitest';

import {
  MAX_SCOPE_PRESETS,
  buildScopePresetId,
  loadScopePresets,
  saveScopePresets,
} from '../scopePresets.js';

const WORKSPACE_ID = 'workspace-test';

function buildPreset(index) {
  return {
    id: `preset-${index}`,
    label: `Preset ${index}`,
    accountAuthId: 'account-1',
    bcId: `bc-${index}`,
    advertiserId: `adv-${index}`,
    storeId: `store-${index}`,
  };
}

describe('scopePresets utilities', () => {
  beforeEach(() => {
    if (typeof window !== 'undefined' && window.localStorage) {
      window.localStorage.clear();
    }
  });

  it('builds deterministic preset ids from scope parts', () => {
    const id = buildScopePresetId({
      accountAuthId: 'acct',
      bcId: 'bc',
      advertiserId: 'adv',
      storeId: 'store',
    });
    expect(id).toBe('acct__bc__adv__store');
  });

  it('persists presets to localStorage and normalizes data on load', () => {
    saveScopePresets(WORKSPACE_ID, [
      {
        id: ' preset-a ',
        label: '  ',
        accountAuthId: 'acct-1',
        bcId: 'bc-1',
        advertiserId: 'adv-1',
        storeId: 'store-1',
      },
    ]);

    const loaded = loadScopePresets(WORKSPACE_ID);

    expect(loaded).toHaveLength(1);
    expect(loaded[0]).toMatchObject({
      id: 'preset-a',
      label: 'acct-1 / bc-1 / adv-1 / store-1',
      accountAuthId: 'acct-1',
      bcId: 'bc-1',
      advertiserId: 'adv-1',
      storeId: 'store-1',
    });
  });

  it('limits saved presets to MAX_SCOPE_PRESETS and ignores invalid entries', () => {
    const manyPresets = Array.from({ length: MAX_SCOPE_PRESETS + 5 }, (_, index) => buildPreset(index));
    manyPresets.push({ id: 'invalid', label: 'Missing ids' });

    saveScopePresets(WORKSPACE_ID, manyPresets);

    const loaded = loadScopePresets(WORKSPACE_ID);

    expect(loaded).toHaveLength(MAX_SCOPE_PRESETS);
    expect(loaded[0].id).toBe('preset-0');
    expect(loaded.at(-1).id).toBe(`preset-${MAX_SCOPE_PRESETS - 1}`);
  });
});
