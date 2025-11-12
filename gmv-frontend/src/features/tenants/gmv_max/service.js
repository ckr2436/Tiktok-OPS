import { listBindings, normProvider } from '../integrations/tiktok_business/service.js';

const tenantPrefix = (wid) => `/api/v1/tenants/${encodeURIComponent(wid)}`;
const providerPrefix = (wid, provider) => `${tenantPrefix(wid)}/providers/${encodeURIComponent(normProvider(provider))}`;
const accountPrefix = (wid, provider, authId) => `${providerPrefix(wid, provider)}/accounts/${encodeURIComponent(authId)}`;

function appendQuery(url, params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value) !== '') {
      search.append(key, value);
    }
  });
  const qs = search.toString();
  return qs ? `${url}?${qs}` : url;
}

async function apiGet(url, { signal, headers } = {}) {
  const response = await fetch(url, { credentials: 'include', signal, headers });
  if (!response.ok) {
    const message = await response.text();
    const error = new Error(message || 'Request failed');
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function apiPut(url, body) {
  const response = await fetch(url, {
    method: 'PUT',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  const text = await response.text();
  if (!response.ok) {
    const error = new Error(text || 'Request failed');
    error.status = response.status;
    throw error;
  }
  return text ? JSON.parse(text) : null;
}

async function apiPost(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  const text = await response.text();
  if (!response.ok) {
    const error = new Error(text || 'Request failed');
    error.status = response.status;
    throw error;
  }
  return text ? JSON.parse(text) : null;
}

export async function fetchBindingConfig(wid, provider, authId, options = {}) {
  const url = `${accountPrefix(wid, provider, authId)}/gmvmax/config`;
  return apiGet(url, options);
}

export async function saveBindingConfig(wid, provider, authId, payload) {
  const url = `${accountPrefix(wid, provider, authId)}/gmvmax/config`;
  return apiPut(url, payload);
}

export async function fetchBusinessCenters(wid, provider, authId, options = {}) {
  const url = `${accountPrefix(wid, provider, authId)}/business-centers`;
  return apiGet(url, options);
}

export async function fetchAdvertisers(wid, provider, authId, params = {}, options = {}) {
  const base = `${accountPrefix(wid, provider, authId)}/advertisers`;
  const normalized = {};
  if (params && typeof params === 'object') {
    if (params.owner_bc_id !== undefined && params.owner_bc_id !== null && params.owner_bc_id !== '') {
      normalized.owner_bc_id = params.owner_bc_id;
    } else if (params.ownerBcId !== undefined && params.ownerBcId !== null && params.ownerBcId !== '') {
      normalized.owner_bc_id = params.ownerBcId;
    }
  }
  return apiGet(appendQuery(base, normalized), options);
}

/**
 * 这里改成和你 curl 一致的后端路由：
 * GET /accounts/{auth_id}/advertisers/{advertiser_id}/stores
 * 可选 owner_bc_id 作为查询参数。
 */
export async function fetchStores(wid, provider, authId, advertiserId, params = {}, options = {}) {
  if (!advertiserId) {
    throw new Error('advertiserId is required to fetch stores');
  }

  const base = `${accountPrefix(wid, provider, authId)}/advertisers/${encodeURIComponent(advertiserId)}/stores`;
  const query = {};

  if (params && typeof params === 'object') {
    if (params.owner_bc_id !== undefined && params.owner_bc_id !== null && params.owner_bc_id !== '') {
      query.owner_bc_id = params.owner_bc_id;
    } else if (params.ownerBcId !== undefined && params.ownerBcId !== null && params.ownerBcId !== '') {
      query.owner_bc_id = params.ownerBcId;
    }
  }

  const url = appendQuery(base, query);
  return apiGet(url, options);
}

/**
 * 商品查询：按 store_id 查询 GMV Max 商品，默认带 product_eligibility=gmv_max，
 * 并且补齐 page / page_size。
 */
export async function fetchProducts(wid, provider, authId, storeId, params = {}, options = {}) {
  if (!storeId) {
    throw new Error('storeId is required to fetch products');
  }

  const base = `${accountPrefix(wid, provider, authId)}/products`;

  const { product_eligibility, eligibility, ...rest } = params || {};
  const query = {
    store_id: storeId,
    product_eligibility: product_eligibility || eligibility || 'gmv_max',
    ...rest,
  };

  delete query.eligibility;

  if (!('page_size' in query)) {
    query.page_size = 10;
  }
  if (!('page' in query)) {
    query.page = 1;
  }

  const url = appendQuery(base, query);
  return apiGet(url, options);
}

export async function fetchGmvOptions(
  wid,
  provider,
  authId,
  { refresh = false, etag, signal } = {},
) {
  const base = `${accountPrefix(wid, provider, authId)}/gmvmax/options`;
  const url = refresh ? appendQuery(base, { refresh: 1 }) : base;
  const headers = {};
  if (etag) {
    headers['If-None-Match'] = etag;
  }
  const response = await fetch(url, { credentials: 'include', signal, headers });
  const nextEtag = response.headers.get('ETag');
  if (response.status === 304) {
    return { status: 304, data: null, etag: nextEtag || etag || null };
  }
  const text = await response.text();
  if (!response.ok) {
    const error = new Error(text || 'Request failed');
    error.status = response.status;
    throw error;
  }
  const payload = text ? JSON.parse(text) : null;
  return { status: response.status, data: payload, etag: nextEtag || null };
}

export async function triggerProductSync(wid, provider, authId, payload) {
  const url = `${accountPrefix(wid, provider, authId)}/sync`;
  const body = {
    scope: 'products',
    mode: payload?.mode || 'full',
    idempotency_key: payload?.idempotency_key || undefined,
    advertiser_id: payload?.advertiserId || payload?.advertiser_id,
    store_id: payload?.storeId || payload?.store_id,
    bc_id: payload?.bcId || payload?.bc_id || undefined,
    product_eligibility: payload?.product_eligibility || payload?.eligibility || 'gmv_max',
  };
  if (!body.advertiser_id || !body.store_id) {
    throw new Error('advertiser_id and store_id are required');
  }
  if (!body.bc_id) delete body.bc_id;
  if (!body.idempotency_key) delete body.idempotency_key;
  return apiPost(url, body);
}

export async function fetchSyncRun(wid, provider, authId, runId, options = {}) {
  const url = `${accountPrefix(wid, provider, authId)}/sync-runs/${encodeURIComponent(runId)}`;
  return apiGet(url, options);
}

export { listBindings, normProvider };

