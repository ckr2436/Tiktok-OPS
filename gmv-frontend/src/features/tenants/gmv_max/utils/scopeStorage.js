const LS_SCOPE_KEY = 'gmv.max.overview.scope.v1';

function getStorage() {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage || null;
  } catch (error) {
    console.warn('Unable to access localStorage for GMV Max scope persistence', error);
    return null;
  }
}

function normalizeAccountId(value) {
  if (value === undefined || value === null) return undefined;
  const stringValue = String(value).trim();
  return stringValue ? stringValue : undefined;
}

function normalizeNullableId(value) {
  if (value === undefined) return undefined;
  if (value === null) return null;
  const stringValue = String(value).trim();
  return stringValue ? stringValue : null;
}

function sanitizeScope(rawScope) {
  if (!rawScope || typeof rawScope !== 'object') return null;
  const workspaceId = rawScope.workspaceId ? String(rawScope.workspaceId).trim() : '';
  if (!workspaceId) return null;
  const provider = rawScope.provider ? String(rawScope.provider).trim() : '';
  const scope = {
    workspaceId,
    provider: provider || 'tiktok-business',
  };

  const accountAuthId = normalizeAccountId(rawScope.accountAuthId);
  if (accountAuthId !== undefined) {
    scope.accountAuthId = accountAuthId;
  }

  if ('businessCenterId' in rawScope) {
    scope.businessCenterId = normalizeNullableId(rawScope.businessCenterId);
  }
  if ('advertiserId' in rawScope) {
    scope.advertiserId = normalizeNullableId(rawScope.advertiserId);
  }
  if ('storeId' in rawScope) {
    scope.storeId = normalizeNullableId(rawScope.storeId);
  }

  return scope;
}

export function loadScope(workspaceId, provider) {
  const storage = getStorage();
  if (!storage) return null;
  if (!workspaceId) return null;
  try {
    const raw = storage.getItem(LS_SCOPE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    const entry = parsed[String(workspaceId)];
    const scope = sanitizeScope(entry);
    if (!scope) return null;
    if (scope.workspaceId !== String(workspaceId)) return null;
    if (provider && scope.provider && scope.provider !== provider) return null;
    return scope;
  } catch (error) {
    console.warn('Failed to read GMV Max scope from localStorage', error);
    return null;
  }
}

function buildNormalizedScope(workspaceId, provider, scope = {}) {
  const normalizedWorkspaceId = workspaceId ? String(workspaceId).trim() : '';
  if (!normalizedWorkspaceId) return null;
  const normalizedProvider = provider ? String(provider).trim() : '';
  const payload = {
    workspaceId: normalizedWorkspaceId,
    provider: normalizedProvider || 'tiktok-business',
  };

  const accountAuthId = normalizeAccountId(scope.accountAuthId);
  if (accountAuthId !== undefined) {
    payload.accountAuthId = accountAuthId;
  }

  if ('businessCenterId' in scope) {
    payload.businessCenterId = normalizeNullableId(scope.businessCenterId);
  }
  if ('advertiserId' in scope) {
    payload.advertiserId = normalizeNullableId(scope.advertiserId);
  }
  if ('storeId' in scope) {
    payload.storeId = normalizeNullableId(scope.storeId);
  }

  return payload;
}

export function saveScope(workspaceId, provider, scope) {
  const storage = getStorage();
  if (!storage) return;
  const normalizedScope = buildNormalizedScope(workspaceId, provider, scope);
  if (!normalizedScope) return;

  try {
    const raw = storage.getItem(LS_SCOPE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    const record = parsed && typeof parsed === 'object' ? parsed : {};
    record[String(normalizedScope.workspaceId)] = normalizedScope;
    storage.setItem(LS_SCOPE_KEY, JSON.stringify(record));
  } catch (error) {
    console.warn('Failed to persist GMV Max scope to localStorage', error);
  }
}

export function clearScope(workspaceId) {
  const storage = getStorage();
  if (!storage) return;
  if (!workspaceId) return;
  try {
    const raw = storage.getItem(LS_SCOPE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return;
    if (!(String(workspaceId) in parsed)) return;
    delete parsed[String(workspaceId)];
    storage.setItem(LS_SCOPE_KEY, JSON.stringify(parsed));
  } catch (error) {
    console.warn('Failed to clear GMV Max scope from localStorage', error);
  }
}

export { LS_SCOPE_KEY };

