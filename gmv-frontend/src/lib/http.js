import axios from 'axios';
import { apiRoot } from '../core/config.js';

function isCanceledRequest(error) {
  if (!error) return false;

  if (error.__cancelledRequest) return true;

  if (typeof axios.isCancel === 'function' && axios.isCancel(error)) return true;
  if (error.code === 'ERR_CANCELED') return true;
  if (error.name === 'CanceledError' || error.name === 'CancelledError' || error.name === 'AbortError') {
    return true;
  }
  if (error?.request?.aborted || error?.config?.signal?.aborted) {
    return true;
  }

  const message = String(error.message || '').toLowerCase();
  if (
    message.includes('aborted') ||
    message.includes('abort') ||
    message.includes('canceled') ||
    message.includes('cancelled')
  ) {
    return true;
  }

  const status = error?.response?.status;
  if (status === 0 || status === 499) return true;

  return false;
}

function markCanceledRequest(error) {
  if (error) {
    // eslint-disable-next-line no-param-reassign
    error.__cancelledRequest = true;
  }
  return error;
}

const http = axios.create({
  baseURL: apiRoot,
  withCredentials: true,
  timeout: 15000,
});

export function safePath(path) {
  if (typeof path !== 'string') return path;
  return path.replace(/\/api\/v1\/api\/v1/g, '/api/v1');
}

function ensureHeader(headers, key, value) {
  if (!headers) return;
  if (typeof headers.set === 'function') {
    if (!headers.has(key)) {
      headers.set(key, value);
    }
    return;
  }
  if (!headers[key]) {
    headers[key] = value;
  }
}

function readHeader(headers, key) {
  if (!headers) return undefined;
  if (typeof headers.get === 'function') {
    return headers.get(key);
  }
  return headers[key];
}

function emitHttpError(type, message, context) {
  const detail = { type, message, context };
  if (typeof window !== 'undefined') {
    const event = new CustomEvent('http:error', {
      detail,
      cancelable: true,
    });
    window.dispatchEvent(event);
  }

  if (console?.error) {
    console.error('[http]', message, context);
  }
}

function extractErrorMessage(response) {
  if (!response) return null;
  const data = response.data;
  if (!data || typeof data !== 'object') return null;
  if (data.error?.message) return data.error.message;
  if (typeof data.message === 'string') return data.message;
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((item) => item?.msg || '').filter(Boolean).join('\n') || null;
  }
  return null;
}

function buildUiMessage(error) {
  const { response } = error;
  const status = response?.status;
  const serverMessage = extractErrorMessage(response);
  if (status === 401) {
    return serverMessage || '登录状态已过期，请重新登录后重试。';
  }
  if (status === 403) {
    return serverMessage || '您没有执行此操作的权限。';
  }
  if (status === 422) {
    return serverMessage || '提交的数据不合法，请检查后再试。';
  }
  if (status === 429) {
    return serverMessage || '请求过于频繁，请稍后再试。';
  }
  if (status && status >= 500) {
    return '服务器暂时不可用，请稍后重试。';
  }
  return serverMessage || '请求失败，请稍后再试。';
}

function isGmvMaxMetricsFilterError(error) {
  const status = error?.response?.status;
  if (status !== 422) return false;
  const messageSource =
    error?.response?.data?.detail || error?.response?.data?.message || error?.message || '';
  const message = String(messageSource).toLowerCase();
  const matchesDetail =
    message.includes('item level metrics requires at least 1 campaign_id filter') ||
    message.includes('creative level metrics requires at least 1 campaign_id and 1 item_group_id');
  if (!matchesDetail) return false;
  const url = error?.config?.url || '';
  return typeof url === 'string' && url.includes('/gmvmax/') && url.includes('/metrics');
}

function shouldRetryGet(error) {
  const config = error?.config;
  if (!config) return false;
  const method = (config.method || '').toLowerCase();
  if (method !== 'get') return false;

  const url = String(config.url || '');
  if (url.includes('/gmvmax/') && url.includes('/metrics')) {
    return false;
  }

  const status = error?.response?.status;
  return !error.response || (status >= 500 && status !== 501);
}

http.interceptors.request.use((cfg) => {
  const headers = cfg.headers ?? (cfg.headers = {});
  if (typeof headers.set === 'function') {
    headers.set('x-client', 'gmv-frontend');
  } else {
    headers['x-client'] = 'gmv-frontend';
  }
  if (typeof cfg.url === 'string') {
    cfg.url = safePath(cfg.url);
  }
  const requestId =
    readHeader(headers, 'x-request-id') ||
    (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : Date.now().toString(36));
  ensureHeader(headers, 'x-request-id', requestId);
  cfg.metadata = {
    ...(cfg.metadata || {}),
    requestId,
  };
  return cfg;
});

http.interceptors.response.use(
  (res) => res,
  async (error) => {
    const config = error.config ?? {};
    error.config = config;
    config.__retryCount = config.__retryCount || 0;

    // ✅ 先判断是否是前端主动取消的请求，直接标记后返回，不再做重试和统一错误处理
    if (isCanceledRequest(error)) {
      return Promise.reject(markCanceledRequest(error));
    }

    // 只有真正的网络错误 / 5xx 才走重试逻辑
    if (shouldRetryGet(error) && config.__retryCount < 2) {
      config.__retryCount += 1;
      const delay = 300 * 2 ** (config.__retryCount - 1);
      await new Promise((resolve) => setTimeout(resolve, delay));
      return http.request(config);
    }

    const requestId =
      config?.metadata?.requestId || readHeader(config?.headers, 'x-request-id');
    const uiMessage = buildUiMessage(error);
    const status = error?.response?.status;
    if (requestId) {
      error.requestId = requestId;
    }
    if (status !== undefined) {
      error.status = status;
    }
    if (error?.response?.data !== undefined) {
      error.payload = error.response.data;
    }
    if (uiMessage) {
      error.message = uiMessage;
    }
    error.uiMessage = uiMessage;
    if (!error?.__cancelledRequest) {
      console.error('[http] request failed', {
        method: config?.method,
        url: config?.url,
        status,
        requestId,
        response: error?.response?.data,
      });
    }

    if (isGmvMaxMetricsFilterError(error)) {
      return Promise.reject(error);
    }

    if (status === 401 || status === 403) {
      emitHttpError('auth', uiMessage, { status, requestId, error });
    } else if (status === 422) {
      emitHttpError('validation', uiMessage, { status, requestId, error });
    } else if (status === 429) {
      emitHttpError('rate-limit', uiMessage, { status, requestId, error });
    } else if (!status || status >= 500) {
      emitHttpError('server', uiMessage, { status, requestId, error });
    } else {
      emitHttpError('http', uiMessage, { status, requestId, error });
    }

    return Promise.reject(error);
  }
);

export default http;
export { http, isCanceledRequest };

