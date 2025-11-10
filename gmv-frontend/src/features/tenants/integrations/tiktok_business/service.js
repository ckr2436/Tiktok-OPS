// TikTok Business integration service helpers

export function normProvider(p) {
  const raw = (p && String(p).trim()) || 'tiktok-business';
  return raw.toLowerCase().replace(/_/g, '-');
}

const tenantPrefix = (wid) => `/api/v1/tenants/${encodeURIComponent(wid)}`;
const oauthPrefix = (wid) => `${tenantPrefix(wid)}/oauth/tiktok-business`;
const providerPrefix = (wid, provider) => `${tenantPrefix(wid)}/providers/${encodeURIComponent(normProvider(provider))}`;
const accountsPrefix = (wid, provider) => `${providerPrefix(wid, provider)}/accounts`;

function appendQuery(url, params = {}) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value) !== '') {
      search.append(key, value);
    }
  });
  const qs = search.toString();
  return qs ? `${url}?${qs}` : url;
}

async function apiGet(url, { signal } = {}) {
  const r = await fetch(url, { credentials: 'include', signal });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function apiPost(url, body) {
  const init = {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function apiPatch(url, body) {
  const r = await fetch(url, {
    method: 'PATCH',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function apiDelete(url) {
  const r = await fetch(url, { method: 'DELETE', credentials: 'include' });
  if (r.status === 204) return null;
  const text = await r.text();
  if (!r.ok) throw new Error(text || 'Request failed');
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    return null;
  }
}

/* ---------- 公司信息 ---------- */
export async function getTenantMeta(wid) {
  return apiGet(`${tenantPrefix(wid)}/meta`);
}

/* ---------- Provider Apps / 授权会话 ---------- */
export async function listProviderApps(wid) {
  const data = await apiGet(`${oauthPrefix(wid)}/provider-apps`);
  return Array.isArray(data?.items) ? data.items : [];
}

export async function createAuthz(wid, { provider_app_id, alias, return_to }) {
  return apiPost(`${oauthPrefix(wid)}/authz`, {
    provider_app_id,
    alias: alias ?? null,
    return_to: return_to ?? null,
  });
}

/* ---------- 绑定列表 ---------- */
export async function listBindings(wid) {
  const data = await apiGet(`${oauthPrefix(wid)}/bindings`);
  return Array.isArray(data?.items) ? data.items : [];
}

export async function getBindingById(wid, auth_id) {
  const list = await listBindings(wid);
  return list.find((x) => String(x.auth_id) === String(auth_id)) || null;
}

/* ---------- 撤销 / 删除 ---------- */
export async function revokeBinding(wid, auth_id, remote = true) {
  return apiPost(
    `${oauthPrefix(wid)}/bindings/${encodeURIComponent(auth_id)}/revoke?remote=${remote ? 'true' : 'false'}`
  );
}

export async function hardDeleteBinding(wid, auth_id) {
  return apiDelete(`${oauthPrefix(wid)}/bindings/${encodeURIComponent(auth_id)}`);
}

/* ---------- 广告主相关（只读 + 设置主） ---------- */
export async function advertisersOf(
  wid,
  auth_id,
  provider = 'tiktok-business',
  params = {},
  options = {}
) {
  const url = appendQuery(
    `${accountsPrefix(wid, provider)}/${encodeURIComponent(auth_id)}/advertisers`,
    params,
  );
  const data = await apiGet(url, options);
  if (Array.isArray(data?.items)) return data.items;
  if (Array.isArray(data)) return data;
  return [];
}

export async function setPrimary(wid, auth_id, advertiser_id) {
  return apiPost(`${oauthPrefix(wid)}/bindings/${encodeURIComponent(auth_id)}/set-primary`, {
    advertiser_id,
  });
}

export async function updateAlias(wid, auth_id, alias) {
  return apiPatch(`${oauthPrefix(wid)}/bindings/${encodeURIComponent(auth_id)}/alias`, {
    alias: alias ?? null,
  });
}

/* ---------- 新业务 / 同步域 ---------- */
export async function listProviderAccounts(wid, provider, params = {}, options = {}) {
  return apiGet(appendQuery(accountsPrefix(wid, provider), params), options);
}

export async function triggerSync(wid, provider, authId, scope = 'all', payload = {}) {
  return apiPost(`${accountsPrefix(wid, provider)}/${encodeURIComponent(authId)}/sync`, {
    scope,
    ...payload,
  });
}

export async function getSyncRun(wid, provider, authId, runId, options = {}) {
  return apiGet(
    `${accountsPrefix(wid, provider)}/${encodeURIComponent(authId)}/sync-runs/${encodeURIComponent(runId)}`,
    options
  );
}

export async function listEntities(wid, provider, authId, entity, params = {}, options = {}) {
  const base = `${accountsPrefix(wid, provider)}/${encodeURIComponent(authId)}/${encodeURIComponent(entity)}`;
  return apiGet(appendQuery(base, params), options);
}

