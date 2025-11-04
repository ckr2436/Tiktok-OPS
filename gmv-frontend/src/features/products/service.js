import { adaptPageInfo, adaptProduct } from './adapters.js';

const ENDPOINT = ['/open_api/v1.3', 'store', 'product', 'get'].join('/') + '/';

const PARAM_KEYS = {
  bcId: 'bc_id',
  storeId: 'store_id',
  advertiserId: 'advertiser_id',
  productName: 'product_name',
  sortField: 'sort_field',
  sortType: 'sort_type',
  page: 'page',
  pageSize: 'page_size',
};

let activeController = null;

function encodeValue(value) {
  if (value === undefined || value === null) return undefined;
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

function buildSearch(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    const encoded = encodeValue(value);
    if (encoded !== undefined) {
      search.append(key, encoded);
    }
  });
  return search.toString();
}

function normalizeParams(params = {}) {
  if (!params || typeof params !== 'object') {
    return {};
  }
  return Object.entries(params).reduce((acc, [key, value]) => {
    const mapped = PARAM_KEYS[key] || key;
    acc[mapped] = value;
    return acc;
  }, {});
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

function resolveItems(payload) {
  const list =
    payload?.data?.list
    ?? payload?.data?.items
    ?? payload?.list
    ?? payload?.items
    ?? [];
  return Array.isArray(list) ? list.map(adaptProduct) : [];
}

function resolvePageInfo(payload) {
  const rawInfo = payload?.data?.page_info || payload?.page_info || payload?.data || {};
  return adaptPageInfo(rawInfo);
}

export function getStoreProducts(params = {}) {
  const normalized = normalizeParams(params);

  if (!normalized.bc_id) {
    throw { message: 'bcId is required', code: 'INVALID_PARAMS' };
  }
  if (!normalized.store_id) {
    throw { message: 'storeId is required', code: 'INVALID_PARAMS' };
  }

  const scope = params.scope;
  delete normalized.scope;
  if (scope === 'GMV_MAX') {
    normalized.ad_creation_eligible = 'GMV_MAX';
    if (!normalized.advertiser_id) {
      throw {
        message: 'advertiserId is required when scope is GMV_MAX',
        code: 'INVALID_PARAMS',
      };
    }
  } else if (params.advertiserId && !normalized.advertiser_id) {
    normalized.advertiser_id = params.advertiserId;
  }

  if (!normalized.page) {
    normalized.page = 1;
  }
  if (!normalized.page_size) {
    normalized.page_size = 20;
  }

  const query = buildSearch(normalized);
  const url = query ? `${ENDPOINT}?${query}` : ENDPOINT;

  if (activeController) {
    activeController.abort();
  }

  const controller = new AbortController();
  activeController = controller;

  const promise = fetch(url, {
    method: 'GET',
    credentials: 'include',
    signal: controller.signal,
  })
    .then(async (response) => {
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
          if (error.name === 'SyntaxError') {
            throw { message: text || '请求失败', code: response.status };
          }
          throw error;
        }
      }

      if (!text) {
        return { items: [], pageInfo: adaptPageInfo({}) };
      }

      let payload;
      try {
        payload = JSON.parse(text);
      } catch (error) {
        throw { message: '响应解析失败', code: 'PARSE_ERROR' };
      }

      return {
        items: resolveItems(payload),
        pageInfo: resolvePageInfo(payload),
      };
    })
    .catch((error) => {
      throw toApiError(error);
    })
    .finally(() => {
      if (activeController === controller) {
        activeController = null;
      }
    });

  const cancel = () => {
    if (!controller.signal.aborted) {
      controller.abort();
    }
  };

  return { promise, cancel };
}

export default { getStoreProducts };
