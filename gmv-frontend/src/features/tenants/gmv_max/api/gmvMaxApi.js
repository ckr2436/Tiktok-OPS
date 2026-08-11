import http from '@/lib/http.js';
import { normProvider } from '@/features/tenants/integrations/tiktok_business/service.js';
import {
  GmvMaxMetricsLevel,
  GMV_MAX_LEVELS_REQUIRING_CAMPAIGN,
  GMV_MAX_LEVELS_REQUIRING_ITEM_GROUP,
} from '../constants/metrics.js';

export function clampPageSize(size, max = 50) {
  const limit = Number.isFinite(Number(max)) && Number(max) > 0 ? Math.floor(Number(max)) : 50;
  const parsed = Number(size);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return limit;
  }
  return Math.max(1, Math.min(Math.floor(parsed), limit));
}

export const GMV_MAX_MAX_AUTO_PAGES = 2000;
export const GMV_MAX_OPERATION_TIMEOUT_MS = 120000;

function throwIfAborted(signal) {
  if (!signal?.aborted) return;
  if (typeof signal.throwIfAborted === 'function') {
    signal.throwIfAborted();
  }
  const error = new Error('GMV Max pagination was aborted.');
  error.name = 'AbortError';
  throw error;
}

function paginationLimitError(maxPages, totalPages) {
  const suffix = Number.isFinite(totalPages) ? ` (reported ${totalPages} pages)` : '';
  const error = new Error(`GMV Max pagination exceeded the ${maxPages}-page safety limit${suffix}.`);
  error.code = 'GMVMAX_PAGINATION_LIMIT_EXCEEDED';
  error.maxPages = maxPages;
  if (Number.isFinite(totalPages)) error.totalPages = totalPages;
  return error;
}

function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

function paginationInvariantError(message, details = {}) {
  const error = new Error(`GMV Max pagination invariant failed: ${message}`);
  error.code = 'GMVMAX_PAGINATION_INVARIANT';
  Object.assign(error, details);
  return error;
}

function definedOwnValue(source, key) {
  if (
    !source ||
    typeof source !== 'object' ||
    !Object.prototype.hasOwnProperty.call(source, key) ||
    source[key] === null ||
    source[key] === undefined
  ) {
    return undefined;
  }
  return source[key];
}

function consistentInteger(label, candidates, { minimum = 0, page } = {}) {
  const values = candidates.filter((value) => value !== undefined);
  if (values.length === 0) return null;
  const parsedValues = values.map(Number);
  if (
    parsedValues.some(
      (value) => !Number.isSafeInteger(value) || value < minimum,
    )
  ) {
    throw paginationInvariantError(`invalid ${label} on page ${page}.`, {
      page,
      field: label,
    });
  }
  if (parsedValues.some((value) => value !== parsedValues[0])) {
    throw paginationInvariantError(`conflicting ${label} values on page ${page}.`, {
      page,
      field: label,
    });
  }
  return parsedValues[0];
}

function consistentBoolean(label, candidates, { page } = {}) {
  const values = candidates.filter((value) => value !== undefined);
  if (values.length === 0) return null;
  if (values.some((value) => typeof value !== 'boolean')) {
    throw paginationInvariantError(`invalid ${label} on page ${page}.`, {
      page,
      field: label,
    });
  }
  if (values.some((value) => value !== values[0])) {
    throw paginationInvariantError(`conflicting ${label} values on page ${page}.`, {
      page,
      field: label,
    });
  }
  return values[0];
}

function resolvePaginationDescriptor(payload, fallbackPage, fallbackPageSize) {
  if (!payload || typeof payload !== 'object') return null;

  let kind = null;
  let items = null;
  let rawPageInfo = null;
  if (Array.isArray(payload.report?.list)) {
    kind = 'report';
    items = payload.report.list;
    rawPageInfo = payload.report.page_info;
  } else if (Array.isArray(payload.items)) {
    kind = 'items';
    items = payload.items;
    rawPageInfo = payload.page_info;
  } else if (Array.isArray(payload.list)) {
    kind = 'list';
    items = payload.list;
    rawPageInfo = payload.page_info;
  } else {
    return null;
  }

  if (
    rawPageInfo !== null &&
    rawPageInfo !== undefined &&
    (typeof rawPageInfo !== 'object' || Array.isArray(rawPageInfo))
  ) {
    throw paginationInvariantError(`invalid page_info on page ${fallbackPage}.`, {
      page: fallbackPage,
      field: 'page_info',
    });
  }
  const pageInfo = rawPageInfo || null;
  const page = consistentInteger(
    'page',
    [
      definedOwnValue(pageInfo, 'page'),
      definedOwnValue(payload, 'page'),
    ],
    { minimum: 1, page: fallbackPage },
  );
  const pageSize = consistentInteger(
    'page_size',
    [
      definedOwnValue(pageInfo, 'page_size'),
      definedOwnValue(payload, 'page_size'),
    ],
    { minimum: 1, page: fallbackPage },
  );
  const total = consistentInteger(
    'total',
    [
      definedOwnValue(pageInfo, 'total_number'),
      definedOwnValue(pageInfo, 'total'),
      definedOwnValue(payload, 'total_number'),
      definedOwnValue(payload, 'total'),
    ],
    { minimum: 0, page: fallbackPage },
  );
  const reportedTotalPages = consistentInteger(
    'total_page',
    [
      definedOwnValue(pageInfo, 'total_page'),
      definedOwnValue(pageInfo, 'total_pages'),
      definedOwnValue(payload, 'total_page'),
      definedOwnValue(payload, 'total_pages'),
    ],
    { minimum: 0, page: fallbackPage },
  );
  const reportedHasNext = consistentBoolean(
    'continuation flag',
    [
      definedOwnValue(pageInfo, 'has_next'),
      definedOwnValue(pageInfo, 'has_more'),
      definedOwnValue(payload, 'has_next'),
      definedOwnValue(payload, 'has_more'),
    ],
    { page: fallbackPage },
  );
  const hasMetadata = [
    page,
    pageSize,
    total,
    reportedTotalPages,
    reportedHasNext,
  ].some((value) => value !== null);
  const effectivePage = page ?? fallbackPage;
  const effectivePageSize = pageSize ?? fallbackPageSize;
  const computedTotalPages =
    total !== null && effectivePageSize > 0
      ? Math.ceil(total / effectivePageSize)
      : null;
  if (
    reportedTotalPages !== null &&
    computedTotalPages !== null &&
    reportedTotalPages !== computedTotalPages
  ) {
    throw paginationInvariantError(
      `reported total_page ${reportedTotalPages} does not match total ${total} and page_size ${effectivePageSize}.`,
      {
        page: fallbackPage,
        field: 'total_page',
        expectedTotalPages: computedTotalPages,
        receivedTotalPages: reportedTotalPages,
      },
    );
  }
  const totalPages = reportedTotalPages ?? computedTotalPages;
  const computedHasNext =
    totalPages !== null ? effectivePage < totalPages : null;
  if (
    reportedHasNext !== null &&
    computedHasNext !== null &&
    reportedHasNext !== computedHasNext
  ) {
    throw paginationInvariantError(
      `continuation flag contradicts total metadata on page ${fallbackPage}.`,
      {
        page: fallbackPage,
        field: 'has_next',
        expectedHasNext: computedHasNext,
        receivedHasNext: reportedHasNext,
      },
    );
  }
  const hasNext = reportedHasNext ?? computedHasNext ?? false;

  return {
    kind,
    items,
    pageInfo,
    hasMetadata,
    hasReportedPage: page !== null,
    hasReportedPageSize: pageSize !== null,
    page: effectivePage,
    pageSize: effectivePageSize,
    total,
    totalPages,
    hasNext,
  };
}

function scalarFieldKey(field) {
  return (item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
    const value = item[field];
    if (
      !['string', 'number'].includes(typeof value) ||
      (typeof value === 'number' && !Number.isFinite(value))
    ) {
      return null;
    }
    const normalized = String(value).trim();
    return normalized ? `${field}:${normalized}` : null;
  };
}

function canonicalKeyValue(value) {
  if (value === null) return 'null';
  if (typeof value === 'string' || typeof value === 'boolean') {
    return JSON.stringify(value);
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    const entries = value.map(canonicalKeyValue);
    if (entries.some((entry) => entry === null)) return null;
    return `[${entries.join(',')}]`;
  }
  if (value && typeof value === 'object') {
    const keys = Object.keys(value).sort();
    const entries = [];
    for (const key of keys) {
      const encoded = canonicalKeyValue(value[key]);
      if (encoded === null) return null;
      entries.push(`${JSON.stringify(key)}:${encoded}`);
    }
    return `{${entries.join(',')}}`;
  }
  return null;
}

function reportDimensionsKey(row) {
  const dimensions = row?.dimensions;
  if (
    !dimensions ||
    typeof dimensions !== 'object' ||
    Array.isArray(dimensions) ||
    Object.keys(dimensions).length === 0
  ) {
    return null;
  }
  const encoded = canonicalKeyValue(dimensions);
  return encoded === null ? null : `dimensions:${encoded}`;
}

function completePageInfo(pageInfo, itemCount, total) {
  return {
    ...(pageInfo && typeof pageInfo === 'object' ? pageInfo : {}),
    page: 1,
    page_size: itemCount,
    total_number: total,
    total_page: itemCount > 0 ? 1 : 0,
    has_more: false,
    has_next: false,
  };
}

function mergeNumberedPageResponses(responses, descriptors) {
  if (responses.length <= 1) return responses[0];

  const first = responses[0];
  const firstDescriptor = descriptors[0];
  const items = descriptors.flatMap((descriptor) => descriptor.items);
  const reportedTotals = descriptors
    .map((descriptor) => descriptor.total)
    .filter((value) => value !== null);
  const total = Math.max(items.length, ...reportedTotals);

  if (firstDescriptor.kind === 'report') {
    return {
      ...first,
      report: {
        ...first.report,
        list: items,
        page_info: completePageInfo(firstDescriptor.pageInfo, items.length, total),
      },
    };
  }

  if (firstDescriptor.kind === 'list') {
    return {
      ...first,
      list: items,
      ...(firstDescriptor.pageInfo
        ? { page_info: completePageInfo(firstDescriptor.pageInfo, items.length, total) }
        : {}),
      ...(Object.prototype.hasOwnProperty.call(first, 'page')
        ? { page: 1, page_size: items.length, total }
        : {}),
    };
  }

  return {
    ...first,
    items,
    ...(firstDescriptor.pageInfo
      ? { page_info: completePageInfo(firstDescriptor.pageInfo, items.length, total) }
      : {}),
    ...(Object.prototype.hasOwnProperty.call(first, 'page')
      ? { page: 1, page_size: items.length, total }
      : {}),
  };
}

/**
 * Fetch every numbered page without allowing a partial result to masquerade as complete.
 *
 * Supported payloads:
 * - { items, page, page_size, total }
 * - { items, page_info }
 * - { list, page, page_size, total }
 * - { report: { list, page_info }, freshness, ... }
 */
export async function fetchAllNumberedPages({
  fetchPage,
  getItemKey,
  itemKeyLabel = 'item key',
  page = 1,
  pageSize = 50,
  maxPages = GMV_MAX_MAX_AUTO_PAGES,
  signal,
}) {
  if (typeof fetchPage !== 'function') {
    throw new TypeError('fetchPage must be a function.');
  }
  if (typeof getItemKey !== 'function') {
    throw new TypeError('getItemKey must be a function for fetch-all pagination.');
  }

  const initialPage = positiveInteger(page, 1);
  if (initialPage !== 1) {
    throw paginationInvariantError('fetch-all pagination must start at page 1.', {
      page: initialPage,
      field: 'page',
    });
  }
  const normalizedPageSize = positiveInteger(pageSize, 50);
  const normalizedMaxPages = positiveInteger(maxPages, GMV_MAX_MAX_AUTO_PAGES);
  const responses = [];
  const descriptors = [];
  const pageSignatures = new Set();
  const seenItemKeys = new Set();
  let requestedPage = initialPage;
  let expectedTotal = null;
  let expectedPageSize = null;
  let expectedTotalPages = null;

  while (true) {
    throwIfAborted(signal);
    const payload = await fetchPage(requestedPage);
    throwIfAborted(signal);
    const descriptor = resolvePaginationDescriptor(payload, requestedPage, normalizedPageSize);
    responses.push(payload);

    if (!descriptor) {
      throw paginationInvariantError(
        `page ${requestedPage} did not return a supported paginated collection.`,
        { page: requestedPage, field: 'items' },
      );
    }
    if (descriptors.length > 0 && descriptor.kind !== descriptors[0].kind) {
      const error = new Error('GMV Max pagination response shape changed between pages.');
      error.code = 'GMVMAX_PAGINATION_SHAPE_CHANGED';
      throw error;
    }
    if (!descriptor.hasMetadata) {
      throw paginationInvariantError(
        `page ${requestedPage} did not include pagination metadata.`,
        { page: requestedPage, field: 'pagination' },
      );
    }
    if (!descriptor.hasReportedPage) {
      throw paginationInvariantError(
        `page ${requestedPage} did not report its page number.`,
        { page: requestedPage, field: 'page' },
      );
    }
    if (!descriptor.hasReportedPageSize) {
      throw paginationInvariantError(
        `page ${requestedPage} did not report page_size.`,
        { page: requestedPage, field: 'page_size' },
      );
    }
    if (descriptor.total === null) {
      throw paginationInvariantError(
        `page ${requestedPage} did not report an authoritative total.`,
        { page: requestedPage, field: 'total' },
      );
    }
    if (expectedTotal === null) {
      expectedTotal = descriptor.total;
    } else if (descriptor.total !== expectedTotal) {
      throw paginationInvariantError(
        `total changed from ${expectedTotal} to ${descriptor.total}.`,
        {
          page: requestedPage,
          field: 'total',
          expectedTotal,
          receivedTotal: descriptor.total,
        },
      );
    }
    if (expectedPageSize === null) {
      expectedPageSize = descriptor.pageSize;
    } else if (descriptor.pageSize !== expectedPageSize) {
      throw paginationInvariantError(
        `page_size changed from ${expectedPageSize} to ${descriptor.pageSize}.`,
        {
          page: requestedPage,
          field: 'page_size',
          expectedPageSize,
          receivedPageSize: descriptor.pageSize,
        },
      );
    }
    if (expectedTotalPages === null) {
      expectedTotalPages = descriptor.totalPages;
    } else if (descriptor.totalPages !== expectedTotalPages) {
      throw paginationInvariantError(
        `total_page changed from ${expectedTotalPages} to ${descriptor.totalPages}.`,
        {
          page: requestedPage,
          field: 'total_page',
          expectedTotalPages,
          receivedTotalPages: descriptor.totalPages,
        },
      );
    }
    if (descriptor.hasReportedPage && descriptor.page !== requestedPage) {
      const error = new Error(
        `GMV Max pagination stalled: requested page ${requestedPage}, received page ${descriptor.page}.`,
      );
      error.code = 'GMVMAX_PAGINATION_STALLED';
      error.requestedPage = requestedPage;
      error.receivedPage = descriptor.page;
      throw error;
    }
    if (
      descriptor.totalPages !== null &&
      descriptor.totalPages - initialPage + 1 > normalizedMaxPages
    ) {
      throw paginationLimitError(normalizedMaxPages, descriptor.totalPages);
    }
    const expectedPageItemCount = Math.max(
      0,
      Math.min(
        descriptor.pageSize,
        descriptor.total - (descriptor.page - 1) * descriptor.pageSize,
      ),
    );
    if (descriptor.items.length !== expectedPageItemCount) {
      throw paginationInvariantError(
        `page ${requestedPage} returned ${descriptor.items.length} rows; metadata requires ${expectedPageItemCount}.`,
        {
          page: requestedPage,
          expectedPageItemCount,
          receivedPageItemCount: descriptor.items.length,
        },
      );
    }
    const itemsSignature =
      descriptor.items.length > 0 ? JSON.stringify(descriptor.items) : null;
    if (itemsSignature !== null && pageSignatures.has(itemsSignature)) {
      const error = new Error(
        `GMV Max pagination stalled: page ${requestedPage} repeated earlier page content.`,
      );
      error.code = 'GMVMAX_PAGINATION_STALLED';
      error.requestedPage = requestedPage;
      error.receivedPage = descriptor.page;
      error.reason = 'repeated_page_content';
      throw error;
    }
    if (itemsSignature !== null) pageSignatures.add(itemsSignature);

    const pageItemKeys = new Set();
    descriptor.items.forEach((item, itemIndex) => {
      let itemKey;
      try {
        itemKey = getItemKey(item);
      } catch (cause) {
        throw paginationInvariantError(
          `invalid ${itemKeyLabel} at page ${requestedPage}, index ${itemIndex}.`,
          {
            page: requestedPage,
            itemIndex,
            field: itemKeyLabel,
            cause,
          },
        );
      }
      if (typeof itemKey !== 'string' || itemKey.length === 0) {
        throw paginationInvariantError(
          `missing ${itemKeyLabel} at page ${requestedPage}, index ${itemIndex}.`,
          {
            page: requestedPage,
            itemIndex,
            field: itemKeyLabel,
            reason: 'missing_item_key',
          },
        );
      }
      if (pageItemKeys.has(itemKey) || seenItemKeys.has(itemKey)) {
        throw paginationInvariantError(
          `duplicate ${itemKeyLabel} ${itemKey} on page ${requestedPage}.`,
          {
            page: requestedPage,
            itemIndex,
            field: itemKeyLabel,
            duplicateKey: itemKey,
            reason: 'duplicate_item_key',
            duplicateScope: pageItemKeys.has(itemKey) ? 'within_page' : 'across_pages',
          },
        );
      }
      pageItemKeys.add(itemKey);
    });
    pageItemKeys.forEach((itemKey) => seenItemKeys.add(itemKey));
    descriptors.push(descriptor);
    const collectedItemCount = seenItemKeys.size;
    if (expectedTotal !== null && collectedItemCount > expectedTotal) {
      throw paginationInvariantError(
        `collected ${collectedItemCount} unique items for total ${expectedTotal}.`,
        {
          page: requestedPage,
          expectedTotal,
          collectedItemCount,
        },
      );
    }

    if (descriptor.items.length === 0 && descriptor.hasMetadata && descriptor.hasNext) {
      const error = new Error(
        `GMV Max pagination invariant failed: page ${requestedPage} was empty while metadata reported another page.`,
      );
      error.code = 'GMVMAX_PAGINATION_INVARIANT';
      error.page = requestedPage;
      throw error;
    }
    if (!descriptor.hasNext) {
      if (collectedItemCount !== expectedTotal) {
        throw paginationInvariantError(
          `pagination ended with ${collectedItemCount} unique items for total ${expectedTotal}.`,
          {
            page: requestedPage,
            expectedTotal,
            collectedItemCount,
            reason: 'total_mismatch',
          },
        );
      }
      break;
    }
    if (responses.length >= normalizedMaxPages) {
      throw paginationLimitError(normalizedMaxPages, descriptor.totalPages);
    }

    requestedPage += 1;
  }

  return mergeNumberedPageResponses(responses, descriptors);
}

function resolveFetchAllOptions(params, fetchAllByDefault) {
  const requestParams = params && typeof params === 'object' ? { ...params } : {};
  const requestedFetchAll = requestParams.fetch_all_pages;
  const requestedMaxPages = requestParams.max_pages;
  delete requestParams.fetch_all_pages;
  delete requestParams.max_pages;
  return {
    requestParams,
    fetchAll:
      requestedFetchAll === undefined
        ? fetchAllByDefault
        : requestedFetchAll !== false && requestedFetchAll !== 0 && requestedFetchAll !== 'false',
    maxPages: positiveInteger(requestedMaxPages, GMV_MAX_MAX_AUTO_PAGES),
  };
}

async function getAllNumberedPages(url, params, config, {
  getItemKey,
  itemKeyLabel,
  pageSize,
  maxPages,
}) {
  return fetchAllNumberedPages({
    page: 1,
    pageSize,
    maxPages,
    signal: config?.signal,
    getItemKey,
    itemKeyLabel,
    fetchPage: (page) =>
      get(
        url,
        mergeConfig(config, {
          ...params,
          page,
          page_size: pageSize,
        }),
      ),
  });
}

function encode(value) {
  return encodeURIComponent(value);
}

function tenantPrefix(workspaceId) {
  return `/tenants/${encode(workspaceId)}`;
}

function providerPrefix(workspaceId, provider) {
  return `${tenantPrefix(workspaceId)}/providers/${encode(normProvider(provider))}`;
}

function accountPrefix(workspaceId, provider, authId) {
  return `${providerPrefix(workspaceId, provider)}/accounts/${encode(authId)}`;
}

function mergeConfig(config = {}, params) {
  if (!params || (typeof params === 'object' && Object.keys(params).length === 0)) {
    return { ...config };
  }
  return {
    ...config,
    params: {
      ...(config.params || {}),
      ...params,
    },
  };
}

async function get(url, config) {
  const response = await http.get(url, config);
  return response.data;
}

async function post(url, body, config) {
  const response = await http.post(url, body, config);
  return response.data;
}

export async function startGmvMaxSync(workspaceId, provider, authId, payload = {}, config) {
  return post(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/sync`, payload, config);
}

export async function getTaskStatus(workspaceId, taskId, config) {
  return get(`${tenantPrefix(workspaceId)}/tasks/${encode(taskId)}`, config);
}

async function put(url, body, config) {
  const response = await http.put(url, body, config);
  return response.data;
}

function normalizeIdList(value) {
  if (value === undefined || value === null) return [];
  const list = Array.isArray(value) ? value : [value];
  const normalized = list
    .map((item) => (item === undefined || item === null ? '' : String(item).trim()))
    .filter(Boolean)
    .filter((item) => item.toLowerCase() !== 'all');
  return Array.from(new Set(normalized)).sort();
}

function appendParams(target, params) {
  if (!params) return;
  if (params instanceof URLSearchParams) {
    params.forEach((value, key) => {
      target.append(key, value);
    });
    return;
  }

  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    if (Array.isArray(value)) {
      value.forEach((item) => {
        if (item === undefined || item === null || item === '') return;
        target.append(key, String(item));
      });
      return;
    }
    target.append(key, String(value));
  });
}

export async function listProviders(workspaceId, config) {
  return get(`${tenantPrefix(workspaceId)}/providers`, config);
}

export async function listAccounts(workspaceId, provider, params, config) {
  const { requestParams, fetchAll, maxPages } = resolveFetchAllOptions(params, true);
  const pageSize = clampPageSize(requestParams.page_size ?? 100, 100);
  const url = `${providerPrefix(workspaceId, provider)}/accounts`;
  if (fetchAll) {
    return getAllNumberedPages(url, requestParams, config, {
      pageSize,
      maxPages,
      getItemKey: scalarFieldKey('auth_id'),
      itemKeyLabel: 'account auth_id',
    });
  }
  return get(url, mergeConfig(config, { ...requestParams, page_size: pageSize }));
}

export async function listBusinessCenters(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/business-centers`, axiosConfig);
}

export async function listAdvertisers(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/advertisers`, axiosConfig);
}

export async function listStores(workspaceId, provider, authId, options = {}, config) {
  const { advertiserId, ...params } = options || {};
  const axiosConfig = mergeConfig(config, params);
  const base = advertiserId
    ? `${accountPrefix(workspaceId, provider, authId)}/advertisers/${encode(advertiserId)}/stores`
    : `${accountPrefix(workspaceId, provider, authId)}/stores`;
  return get(base, axiosConfig);
}

export async function listProducts(workspaceId, provider, authId, params, config) {
  const { requestParams, fetchAll, maxPages } = resolveFetchAllOptions(params, true);
  const pageSize = clampPageSize(requestParams.page_size ?? 500, 500);
  const url = `${accountPrefix(workspaceId, provider, authId)}/products`;
  if (fetchAll) {
    return getAllNumberedPages(url, requestParams, config, {
      pageSize,
      maxPages,
      getItemKey: scalarFieldKey('product_id'),
      itemKeyLabel: 'product_id',
    });
  }
  return get(url, mergeConfig(config, { ...requestParams, page_size: pageSize }));
}

export async function getGmvMaxIdentities(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/identity`, axiosConfig);
}

export async function syncAccountMetadata(workspaceId, provider, authId, payload = {}, config) {
  return post(`${accountPrefix(workspaceId, provider, authId)}/sync`, payload, config);
}

export async function syncAccountProducts(workspaceId, provider, authId, payload = {}, config) {
  return post(`${accountPrefix(workspaceId, provider, authId)}/sync`, payload, config);
}

export async function getAccountSyncRun(workspaceId, provider, authId, runId, config) {
  return get(
    `${accountPrefix(workspaceId, provider, authId)}/sync-runs/${encode(runId)}`,
    config,
  );
}

function waitForDelay(delayMs, signal) {
  if (!delayMs) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let timeoutId;
    const cleanup = () => signal?.removeEventListener('abort', abort);
    const abort = () => {
      clearTimeout(timeoutId);
      cleanup();
      const error = new Error('同步状态查询已取消。');
      error.name = 'AbortError';
      reject(error);
    };
    timeoutId = setTimeout(() => {
      cleanup();
      resolve();
    }, delayMs);
    if (!signal) return;
    if (signal.aborted) {
      abort();
      return;
    }
    signal.addEventListener('abort', abort, { once: true });
  });
}

export async function waitForAccountSyncRun(
  workspaceId,
  provider,
  authId,
  runId,
  options = {},
) {
  const {
    timeoutMs = 120000,
    pollIntervalMs = 750,
    signal,
  } = options;
  const startedAt = Date.now();
  while (true) {
    const run = await getAccountSyncRun(workspaceId, provider, authId, runId, { signal });
    const state = String(run?.status || '').trim().toLowerCase();
    if (['success', 'failed', 'partial'].includes(state)) return run;
    if (Date.now() - startedAt >= timeoutMs) {
      const error = new Error('等待后台同步结果超时。');
      error.code = 'ACCOUNT_SYNC_STATUS_TIMEOUT';
      throw error;
    }
    await waitForDelay(pollIntervalMs, signal);
  }
}

export async function getGmvMaxOptions(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/options`, axiosConfig);
}

export async function getGmvMaxConfig(workspaceId, provider, authId, config) {
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/config`, config);
}

export async function updateGmvMaxConfig(workspaceId, provider, authId, payload, config) {
  return put(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/config`, payload, config);
}

export async function getGmvMaxBindingStatus(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/binding_status`, axiosConfig);
}

export async function autoDiscoverGmvMaxBinding(workspaceId, provider, authId, payload, config) {
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/binding/auto`,
    payload,
    config,
  );
}

export async function rebindAutoGmvMaxBinding(workspaceId, provider, authId, payload, config) {
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/rebind_auto`,
    payload,
    config,
  );
}

export async function syncAdvertiserBalance(workspaceId, provider, authId, payload, config) {
  return post(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/balance/sync`, payload, config);
}

export async function syncGmvMaxCampaigns(workspaceId, provider, authId, payload, config) {
  return post(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/sync`, payload, config);
}

export async function getGmvMaxSyncInterval(workspaceId, provider, authId, config) {
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/sync-interval`, config);
}

export async function updateGmvMaxSyncInterval(workspaceId, provider, authId, payload, config) {
  return put(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/sync-interval`, payload, config);
}

export async function getGmvMaxSyncStatus(workspaceId, provider, authId, taskId, config) {
  return get(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/sync/${encode(taskId)}`,
    config,
  );
}

export async function getGmvMaxTaskStatus(workspaceId, provider, authId, taskIdOrUrl, config) {
  const baseUrl = `${accountPrefix(workspaceId, provider, authId)}/gmvmax/tasks`;
  const normalizedTask = taskIdOrUrl ? String(taskIdOrUrl).trim() : '';
  const targetUrl =
    normalizedTask.startsWith('/') || normalizedTask.startsWith('http')
      ? normalizedTask
      : `${baseUrl}/${encode(normalizedTask)}`;
  return get(targetUrl, config);
}

export async function listGmvMaxCampaigns(workspaceId, provider, authId, params, config) {
  const { requestParams, fetchAll, maxPages } = resolveFetchAllOptions(params, true);
  const pageSize = clampPageSize(requestParams.page_size ?? 50, 50);
  const url = `${accountPrefix(workspaceId, provider, authId)}/gmvmax`;
  if (fetchAll) {
    return getAllNumberedPages(url, requestParams, config, {
      pageSize,
      maxPages,
      getItemKey: scalarFieldKey('campaign_id'),
      itemKeyLabel: 'campaign_id',
    });
  }
  return get(url, mergeConfig(config, { ...requestParams, page_size: pageSize }));
}

export async function listGmvMaxHermesDailyReports(workspaceId, provider, authId, params, config) {
  const { requestParams, fetchAll, maxPages } = resolveFetchAllOptions(params, false);
  const pageSize = clampPageSize(requestParams.page_size ?? requestParams.limit ?? 14, 60);
  delete requestParams.limit;
  const url = `${accountPrefix(workspaceId, provider, authId)}/gmvmax/hermes/daily-reports`;
  if (fetchAll) {
    return getAllNumberedPages(url, requestParams, config, {
      pageSize,
      maxPages,
      getItemKey: scalarFieldKey('id'),
      itemKeyLabel: 'Hermes report id',
    });
  }
  return get(url, mergeConfig(config, { ...requestParams, page_size: pageSize }));
}

export async function createGmvMaxCampaign(workspaceId, provider, authId, payload, config) {
  const requestedTimeout = Number(config?.timeout);
  const timeout = Number.isFinite(requestedTimeout)
    ? Math.max(requestedTimeout, GMV_MAX_OPERATION_TIMEOUT_MS)
    : GMV_MAX_OPERATION_TIMEOUT_MS;
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax`,
    payload,
    { ...(config || {}), timeout },
  );
}

export async function precheckGmvMaxCampaign(workspaceId, provider, authId, payload, config) {
  const requestedTimeout = Number(config?.timeout);
  const timeout = Number.isFinite(requestedTimeout)
    ? Math.max(requestedTimeout, GMV_MAX_OPERATION_TIMEOUT_MS)
    : GMV_MAX_OPERATION_TIMEOUT_MS;
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/precheck`,
    payload,
    { ...(config || {}), timeout },
  );
}

export async function listGmvMaxCreativeAssets(workspaceId, provider, authId, params, config) {
  const { requestParams, fetchAll, maxPages } = resolveFetchAllOptions(params, false);
  const pageSize = clampPageSize(requestParams.page_size ?? 24, 100);
  const url = `${accountPrefix(workspaceId, provider, authId)}/gmvmax/creative-assets`;
  const requestConfig = {
    ...(config || {}),
    // FastAPI list query parameters use repeated names. Axios otherwise emits
    // ``item_group_ids[]``, which silently drops the campaign product filter.
    paramsSerializer: config?.paramsSerializer || { indexes: null },
  };
  if (fetchAll) {
    return getAllNumberedPages(url, requestParams, requestConfig, {
      pageSize,
      maxPages,
      getItemKey: scalarFieldKey('item_id'),
      itemKeyLabel: 'creative item_id',
    });
  }
  return get(url, mergeConfig(requestConfig, { ...requestParams, page_size: pageSize }));
}

export async function refreshGmvMaxCreativeAssets(workspaceId, provider, authId, params, config) {
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/creative-assets/refresh`,
    {},
    mergeConfig(config, params),
  );
}

export async function uploadGmvMaxCreativeAsset(workspaceId, provider, authId, formData, config) {
  const response = await http.post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/creative-assets/upload`,
    formData,
    {
      ...(config || {}),
      headers: {
        ...(config?.headers || {}),
        'Content-Type': 'multipart/form-data',
      },
    },
  );
  return response.data;
}

export async function getGmvMaxCampaign(workspaceId, provider, authId, campaignId, config) {
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}`, config);
}

export async function updateGmvMaxCampaign(workspaceId, provider, authId, campaignId, payload, config) {
  return put(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/campaigns/${encode(campaignId)}`, payload, config);
}

export async function syncGmvMaxMetrics(workspaceId, provider, authId, campaignId, payload, config) {
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/campaigns/${encode(campaignId)}/metrics/sync`,
    payload,
    config,
  );
}

export async function getGmvMaxMetrics(workspaceId, provider, authId, campaignId, params, config) {
  const { requestParams, fetchAll, maxPages } = resolveFetchAllOptions(params, true);
  const normalizedParams = { ...requestParams };
  const levelValue = normalizedParams.level ?? GmvMaxMetricsLevel.CAMPAIGN;
  normalizedParams.level = levelValue;

  const campaignIds = normalizeIdList(
    normalizedParams.campaign_ids ?? normalizedParams.campaign_id,
  );
  const itemGroupIds = normalizeIdList(
    normalizedParams.item_group_ids ?? normalizedParams.item_group_id,
  );

  const needsCampaignFilter = GMV_MAX_LEVELS_REQUIRING_CAMPAIGN.has(levelValue);
  const needsItemGroupFilter = GMV_MAX_LEVELS_REQUIRING_ITEM_GROUP.has(levelValue);
  if (needsCampaignFilter && campaignIds.length === 0 && campaignId) {
    campaignIds.push(String(campaignId));
  }
  if (needsItemGroupFilter && itemGroupIds.length === 0 && normalizedParams.item_group_id) {
    itemGroupIds.push(String(normalizedParams.item_group_id));
  }

  const pageSize = clampPageSize(normalizedParams.page_size ?? 1000, 1000);
  const requestedPage = positiveInteger(normalizedParams.page, 1);
  delete normalizedParams.page;
  delete normalizedParams.page_size;

  const effectiveParams = {
    ...normalizedParams,
    campaign_ids: campaignIds.length ? campaignIds : undefined,
    item_group_ids: itemGroupIds.length ? itemGroupIds : undefined,
  };
  delete effectiveParams.campaign_id;
  delete effectiveParams.item_group_id;

  const basePath = `${accountPrefix(workspaceId, provider, authId)}/gmvmax`;
  const metricsPath = campaignId ? `${basePath}/${encode(campaignId)}/metrics` : `${basePath}/metrics`;
  const fetchPage = (page) => {
    const searchParams = new URLSearchParams();
    appendParams(searchParams, config?.params);
    appendParams(searchParams, {
      ...effectiveParams,
      page,
      page_size: pageSize,
    });
    return get(metricsPath, {
      ...(config || {}),
      params: searchParams,
    });
  };

  if (fetchAll) {
    return fetchAllNumberedPages({
      fetchPage,
      page: 1,
      pageSize,
      maxPages,
      signal: config?.signal,
      getItemKey: reportDimensionsKey,
      itemKeyLabel: 'report dimensions',
    });
  }
  return fetchPage(requestedPage);
}

export async function applyGmvMaxAction(workspaceId, provider, authId, campaignId, payload, config) {
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/actions`,
    payload,
    config,
  );
}

export async function listGmvMaxCreativeHeating(workspaceId, provider, authId, campaignId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/creatives/heating`,
    axiosConfig,
  );
}

export async function startGmvMaxCreativeHeating(
  workspaceId,
  provider,
  authId,
  campaignId,
  creativeId,
  payload,
  config,
) {
  const body = {
    action_type: 'BOOST_CREATIVE',
    creative_id: creativeId,
    ...payload,
  };
  return applyGmvMaxAction(workspaceId, provider, authId, campaignId, body, config);
}

export async function stopGmvMaxCreativeHeating(
  workspaceId,
  provider,
  authId,
  campaignId,
  creativeId,
  payload,
  config,
) {
  const body = {
    action_type: 'BOOST_CREATIVE',
    creative_id: creativeId,
    mode: 'STOP',
    ...payload,
  };
  return applyGmvMaxAction(workspaceId, provider, authId, campaignId, body, config);
}

export async function listGmvMaxActionLogs(workspaceId, provider, authId, campaignId, params, config) {
  const sanitizedParams = params && 'page_size' in params
    ? { ...params, page_size: clampPageSize(params.page_size, 200) }
    : params;
  const axiosConfig = mergeConfig(config, sanitizedParams);
  return get(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/actions`,
    axiosConfig,
  );
}

export async function getGmvMaxStrategy(workspaceId, provider, authId, campaignId, config) {
  return get(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/strategy`,
    config,
  );
}

export async function updateGmvMaxStrategy(workspaceId, provider, authId, campaignId, payload, config) {
  return put(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/strategy`,
    payload,
    config,
  );
}

export async function previewGmvMaxStrategy(workspaceId, provider, authId, campaignId, payload, config) {
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/strategies/preview`,
    payload,
    config,
  );
}

export { normalizeIdList };
