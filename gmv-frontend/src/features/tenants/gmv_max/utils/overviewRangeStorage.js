const STORAGE_KEY = 'gmvmax.overview.range.v1';

function getStorage() {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage || null;
  } catch (error) {
    console.warn('Unable to access localStorage for GMV Max overview range persistence', error);
    return null;
  }
}

function normalizePart(value) {
  if (value === undefined || value === null) return '';
  return String(value).trim();
}

function buildScopeKey(workspaceId, provider, authId, storeId) {
  const workspace = normalizePart(workspaceId);
  const normalizedProvider = normalizePart(provider) || 'tiktok-business';
  const account = normalizePart(authId);
  const store = normalizePart(storeId);
  if (!workspace || !account || !store) return null;
  return [workspace, normalizedProvider, account, store].join('::');
}

function normalizeRange(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const rangeKey = raw.rangeKey ? String(raw.rangeKey).trim() : '';
  const customRange = raw.customRange && typeof raw.customRange === 'object'
    ? {
        start: raw.customRange.start ? String(raw.customRange.start).trim() : '',
        end: raw.customRange.end ? String(raw.customRange.end).trim() : '',
      }
    : { start: '', end: '' };

  return {
    rangeKey: rangeKey || 'today',
    customRange,
  };
}

export function loadOverviewRange(workspaceId, provider, authId, storeId) {
  const storage = getStorage();
  const scopeKey = buildScopeKey(workspaceId, provider, authId, storeId);
  if (!storage || !scopeKey) return null;
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    return normalizeRange(parsed[scopeKey]);
  } catch (error) {
    console.warn('Failed to read GMV Max overview range from localStorage', error);
    return null;
  }
}

export function saveOverviewRange(workspaceId, provider, authId, storeId, range) {
  const storage = getStorage();
  const scopeKey = buildScopeKey(workspaceId, provider, authId, storeId);
  if (!storage || !scopeKey) return;
  const normalizedRange = normalizeRange(range);
  if (!normalizedRange) return;

  try {
    const raw = storage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    const record = parsed && typeof parsed === 'object' ? parsed : {};
    record[scopeKey] = normalizedRange;
    storage.setItem(STORAGE_KEY, JSON.stringify(record));
  } catch (error) {
    console.warn('Failed to persist GMV Max overview range to localStorage', error);
  }
}

export function clearOverviewRange(workspaceId, provider, authId, storeId) {
  const storage = getStorage();
  const scopeKey = buildScopeKey(workspaceId, provider, authId, storeId);
  if (!storage || !scopeKey) return;
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || !(scopeKey in parsed)) return;
    delete parsed[scopeKey];
    storage.setItem(STORAGE_KEY, JSON.stringify(parsed));
  } catch (error) {
    console.warn('Failed to clear GMV Max overview range from localStorage', error);
  }
}

export { STORAGE_KEY as LS_OVERVIEW_RANGE_KEY };
