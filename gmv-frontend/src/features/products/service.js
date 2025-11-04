import { mapPageInfo, mapStoreProduct } from './adapters.js';

const ENDPOINT = ['/open_api/v1.3', 'store', 'product', 'get'].join('/') + '/';

function encodeParamValue(value) {
  if (value === undefined || value === null) {
    return undefined;
  }
  if (Array.isArray(value) || typeof value === 'object') {
    try {
      return encodeURIComponent(JSON.stringify(value));
    } catch (error) {
      return encodeURIComponent(String(value));
    }
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed ? trimmed : undefined;
  }
  return String(value);
}

function buildQuery(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, rawValue]) => {
    const value = encodeParamValue(rawValue);
    if (value !== undefined) {
      search.append(key, value);
    }
  });
  return search.toString();
}

async function requestProducts(params, { signal } = {}) {
  const query = buildQuery(params);
  const url = query ? `${ENDPOINT}?${query}` : ENDPOINT;

  const response = await fetch(url, {
    method: 'GET',
    credentials: 'include',
    signal,
  });

  const text = await response.text();

  if (!response.ok) {
    if (!text) {
      throw { message: '请求失败', code: response.status };
    }
    try {
      const payload = JSON.parse(text);
      throw {
        message: payload?.message || payload?.error?.message || '请求失败',
        code: payload?.code || payload?.error?.code || response.status,
      };
    } catch (error) {
      if (error?.name === 'SyntaxError') {
        throw { message: text || '请求失败', code: response.status };
      }
      throw error;
    }
  }

  if (!text) {
    return { items: [], pageInfo: mapPageInfo({}) };
  }

  let payload;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    throw { message: '响应解析失败', code: 'PARSE_ERROR' };
  }

  const list = Array.isArray(payload?.data?.list)
    ? payload.data.list
    : Array.isArray(payload?.data?.items)
      ? payload.data.items
      : Array.isArray(payload?.list)
        ? payload.list
        : Array.isArray(payload?.items)
          ? payload.items
          : [];

  const pageInfoRaw = payload?.data?.page_info || payload?.page_info || payload?.data || {};

  return {
    items: list.map((item) => mapStoreProduct(item)),
    pageInfo: mapPageInfo(pageInfoRaw),
  };
}

function assertBaseParams({ bcId, storeId }) {
  if (!bcId) {
    throw { message: 'bcId is required', code: 'INVALID_PARAMS' };
  }
  if (!storeId) {
    throw { message: 'storeId is required', code: 'INVALID_PARAMS' };
  }
}

export async function fetchStoreProducts({
  bcId,
  storeId,
  page = 1,
  pageSize = 50,
  productName,
  sortField,
  sortType,
  signal,
}) {
  assertBaseParams({ bcId, storeId });

  const params = {
    bc_id: bcId,
    store_id: storeId,
    page,
    page_size: pageSize,
    product_name: productName,
    sort_field: sortField,
    sort_type: sortType,
  };

  return requestProducts(params, { signal });
}

export async function fetchGmvMaxEligibleProducts({
  bcId,
  storeId,
  advertiserId,
  page = 1,
  pageSize = 50,
  productName,
  sortField,
  sortType,
  signal,
}) {
  assertBaseParams({ bcId, storeId });

  if (!advertiserId) {
    throw { message: 'advertiserId is required', code: 'INVALID_PARAMS' };
  }

  const params = {
    bc_id: bcId,
    store_id: storeId,
    advertiser_id: advertiserId,
    ad_creation_eligible: 'GMV_MAX',
    page,
    page_size: pageSize,
    product_name: productName,
    sort_field: sortField,
    sort_type: sortType,
  };

  return requestProducts(params, { signal });
}
