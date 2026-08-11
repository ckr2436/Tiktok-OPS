const STORAGE_PREFIX = 'gmvmax.scopePresets.v1.';
export const MAX_SCOPE_PRESETS = 10;

function getStorage() {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage || null;
  } catch (error) {
    console.warn('Unable to access localStorage for GMV Max scope presets', error);
    return null;
  }
}

function buildKey(workspaceId) {
  if (!workspaceId && workspaceId !== 0) return null;
  return `${STORAGE_PREFIX}${workspaceId}`;
}

function normalizeId(value) {
  if (value === undefined || value === null) return null;
  const stringValue = String(value).trim();
  return stringValue ? stringValue : null;
}

function normalizePreset(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const accountAuthId = normalizeId(raw.accountAuthId);
  const bcId = normalizeId(raw.bcId ?? raw.businessCenterId);
  const advertiserId = normalizeId(raw.advertiserId);
  const storeId = normalizeId(raw.storeId);
  const id = raw.id ? String(raw.id).trim() : null;
  const label = raw.label ? String(raw.label).trim() : '';
  if (!accountAuthId || !bcId || !advertiserId || !storeId || !id) {
    return null;
  }
  return {
    id,
    label: label || `${accountAuthId} / ${bcId} / ${advertiserId} / ${storeId}`,
    accountAuthId,
    bcId,
    advertiserId,
    storeId,
  };
}

export function buildScopePresetId({ accountAuthId, bcId, advertiserId, storeId }) {
  return [accountAuthId, bcId, advertiserId, storeId].map((part) => String(part || '')).join('__');
}

export function loadScopePresets(workspaceId) {
  const storage = getStorage();
  const key = buildKey(workspaceId);
  if (!storage || !key) return [];
  try {
    const raw = storage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map(normalizePreset).filter(Boolean).slice(0, MAX_SCOPE_PRESETS);
  } catch (error) {
    console.warn('Failed to read GMV Max scope presets', error);
    return [];
  }
}

export function saveScopePresets(workspaceId, presets) {
  const storage = getStorage();
  const key = buildKey(workspaceId);
  if (!storage || !key) return;
  const normalized = Array.isArray(presets)
    ? presets.map(normalizePreset).filter(Boolean).slice(0, MAX_SCOPE_PRESETS)
    : [];
  try {
    storage.setItem(key, JSON.stringify(normalized));
  } catch (error) {
    console.warn('Failed to persist GMV Max scope presets', error);
  }
}
