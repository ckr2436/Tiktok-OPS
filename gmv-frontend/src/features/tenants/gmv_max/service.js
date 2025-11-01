import {
  listBindings,
  normProvider,
} from '../integrations/tiktok_business/service.js';

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

async function apiGet(url, { signal } = {}) {
  const response = await fetch(url, { credentials: 'include', signal });
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

export async function fetchBusinessCenters(wid, provider, authId, options = {}) {
  const url = `${accountPrefix(wid, provider, authId)}/business-centers`;
  const data = await apiGet(url, options);
  return Array.isArray(data?.items) ? data.items : [];
}

export async function fetchAdvertisers(wid, provider, authId, params = {}, options = {}) {
  const base = `${accountPrefix(wid, provider, authId)}/advertisers`;
  const data = await apiGet(appendQuery(base, params), options);
  return Array.isArray(data?.items) ? data.items : [];
}

export async function fetchStores(wid, provider, authId, params = {}, options = {}) {
  const base = `${accountPrefix(wid, provider, authId)}/stores`;
  const data = await apiGet(appendQuery(base, params), options);
  return Array.isArray(data?.items) ? data.items : [];
}

export async function fetchBindingConfig(wid, provider, authId, options = {}) {
  const url = `${accountPrefix(wid, provider, authId)}/gmv-max/config`;
  return apiGet(url, options);
}

export async function saveBindingConfig(wid, provider, authId, payload) {
  const url = `${accountPrefix(wid, provider, authId)}/gmv-max/config`;
  return apiPut(url, payload);
}

export async function triggerMetaSync(wid, provider, authId) {
  const url = `${accountPrefix(wid, provider, authId)}/sync`;
  return apiPost(url, { scope: 'meta', mode: 'full', product_eligibility: 'gmv_max' });
}

export async function triggerProductSync(wid, provider, authId, payload) {
  const url = `${accountPrefix(wid, provider, authId)}/sync`;
  return apiPost(url, payload);
}

export async function fetchSyncRun(wid, provider, authId, runId, options = {}) {
  const url = `${accountPrefix(wid, provider, authId)}/sync-runs/${encodeURIComponent(runId)}`;
  return apiGet(url, options);
}

export { listBindings, normProvider };
