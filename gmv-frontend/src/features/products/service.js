const ENDPOINT = ['/open_api/v1.3', 'store', 'product', 'get'].join('/') + '/';

const KEY_MAP = {
  bcId: 'bc_id',
  storeId: 'store_id',
  advertiserId: 'advertiser_id',
  adCreationEligible: 'ad_creation_eligible',
  productName: 'product_name',
  sortField: 'sort_field',
  sortType: 'sort_type',
  pageSize: 'page_size',
  page: 'page',
};

function normalizeParams(params = {}) {
  if (!params || typeof params !== 'object') return {};
  return Object.entries(params).reduce((acc, [key, value]) => {
    const mapped = KEY_MAP[key] || key;
    acc[mapped] = value;
    return acc;
  }, {});
}

function encodeValue(value) {
  if (value === undefined || value === null) return undefined;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed === '' ? undefined : trimmed;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  if (typeof value === 'object') {
    try {
      return encodeURIComponent(JSON.stringify(value));
    } catch (error) {
      return encodeURIComponent(String(value));
    }
  }
  return String(value);
}

function buildQuery(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    const encoded = encodeValue(value);
    if (encoded !== undefined) {
      search.append(key, encoded);
    }
  });
  return search.toString();
}

function toApiError(error, fallbackCode = 'REQUEST_FAILED') {
  if (!error) {
    return { message: '请求失败', code: fallbackCode };
  }
  if (error?.name === 'AbortError') {
    return error;
  }
  if (typeof error === 'object' && 'message' in error && 'code' in error) {
    return error;
  }
  return {
    message: typeof error.message === 'string' && error.message ? error.message : '请求失败',
    code: error.code || error.status || fallbackCode,
  };
}

export async function fetchProducts(params = {}, options = {}) {
  const normalized = normalizeParams(params);
  const requiredKeys = ['bc_id', 'store_id', 'advertiser_id'];

  requiredKeys.forEach((key) => {
    if (!normalized[key]) {
      throw { message: `${key} is required`, code: 'INVALID_PARAMS' };
    }
  });

  if (!normalized.ad_creation_eligible) {
    normalized.ad_creation_eligible = 'GMV_MAX';
  }

  if (!normalized.page) {
    normalized.page = 1;
  }
  if (!normalized.page_size) {
    normalized.page_size = 20;
  }

  const query = buildQuery(normalized);
  const url = query ? `${ENDPOINT}?${query}` : ENDPOINT;
  const { signal } = options;

  let response;
  try {
    response = await fetch(url, { method: 'GET', credentials: 'include', signal });
  } catch (error) {
    throw toApiError(error);
  }

  let text;
  try {
    text = await response.text();
  } catch (error) {
    text = '';
  }

  if (!response.ok) {
    let message = text || '请求失败';
    let code = response.status;
    try {
      const payload = text ? JSON.parse(text) : null;
      if (payload) {
        message = payload?.message || payload?.error?.message || message;
        code = payload?.code || payload?.error?.code || code;
      }
    } catch (error) {
      // ignore json parse error
    }
    throw { message, code };
  }

  if (!text) {
    return {};
  }

  try {
    return JSON.parse(text);
  } catch (error) {
    return {};
  }
}

export default { fetchProducts };
