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
  const url = `${accountPrefix(wid, provider, authId)}/gmv-max/config`;
  return apiGet(url, options);
}

export async function saveBindingConfig(wid, provider, authId, payload) {
  const url = `${accountPrefix(wid, provider, authId)}/gmv-max/config`;
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

export async function fetchStores(wid, provider, authId, advertiserId, params = {}, options = {}) {
  const base = `${accountPrefix(wid, provider, authId)}/stores`;
  const query = { advertiser_id: advertiserId };
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

export async function fetchProducts(wid, provider, authId, storeId, params = {}, options = {}) {
  const base = `${accountPrefix(wid, provider, authId)}/products`;
  const query = { store_id: storeId, ...params };
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
  const base = `${accountPrefix(wid, provider, authId)}/gmv-max/options`;
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
    idempotency_key: payload?.idempotency_key,
    options: {
      advertiser_id: payload?.advertiserId || payload?.advertiser_id,
      store_id: payload?.storeId || payload?.store_id,
      eligibility: payload?.eligibility || payload?.product_eligibility || 'gmv_max',
      bc_id: payload?.bcId || payload?.bc_id || undefined,
    },
  };
  if (!body.options.advertiser_id || !body.options.store_id) {
    throw new Error('advertiser_id and store_id are required');
  }
  if (!body.idempotency_key) {
    delete body.idempotency_key;
  }
  if (!body.options.bc_id) {
    delete body.options.bc_id;
  }
  return apiPost(url, body);
}

export async function fetchSyncRun(wid, provider, authId, runId, options = {}) {
  const url = `${accountPrefix(wid, provider, authId)}/sync-runs/${encodeURIComponent(runId)}`;
  return apiGet(url, options);
}

export { listBindings, normProvider };
