import http from '@/lib/http.js';
import { normProvider } from '@/features/tenants/integrations/tiktok_business/service.js';


const encode = encodeURIComponent;
const VIDEO_UPLOAD_TIMEOUT_MS = 11 * 60 * 1000;
const VIDEO_UPLOAD_POLL_INTERVAL_MS = 2500;

function prefix(workspaceId, provider, authId) {
  return `/tenants/${encode(workspaceId)}/providers/${encode(normProvider(provider))}/accounts/${encode(authId)}/website-ads`;
}

export async function listConnections(workspaceId, provider, authId) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/connections`)).data;
}

export async function createConnection(workspaceId, provider, authId, payload) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/connections`, payload)).data;
}

export async function syncConnection(workspaceId, provider, authId, connectionId) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/connections/${encode(connectionId)}/sync`)).data;
}

export async function listLandingPages(workspaceId, provider, authId) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/landing-pages`)).data;
}

export async function listWebsiteAdsContentProducts(workspaceId, provider, authId) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/content-products`)).data;
}

export async function createManualLandingPage(workspaceId, provider, authId, payload) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/landing-pages/manual`, payload)).data;
}

export async function updateWebsiteAdsProduct(workspaceId, provider, authId, productId, payload) {
  return (await http.patch(`${prefix(workspaceId, provider, authId)}/products/${encode(productId)}`, payload)).data;
}

export async function analyzeWebsiteAdsProduct(workspaceId, provider, authId, productId) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/products/${encode(productId)}/analyze`)).data;
}

export async function getWebsiteAdsMetadata(workspaceId, provider, authId, advertiserId) {
  return (
    await http.get(`${prefix(workspaceId, provider, authId)}/metadata`, {
      params: advertiserId ? { advertiser_id: advertiserId } : {},
    })
  ).data;
}

export async function uploadWebsiteAdsVideoByUrl(workspaceId, provider, authId, payload) {
  return (
    await http.post(`${prefix(workspaceId, provider, authId)}/videos/upload-url`, payload, {
      timeout: VIDEO_UPLOAD_TIMEOUT_MS,
    })
  ).data;
}

export async function uploadWebsiteAdsVideoFile(workspaceId, provider, authId, formData) {
  return (
    await http.post(`${prefix(workspaceId, provider, authId)}/videos/upload-file`, formData, {
      timeout: VIDEO_UPLOAD_TIMEOUT_MS,
    })
  ).data;
}

export async function listWebsiteAdsVideoUploads(
  workspaceId,
  provider,
  authId,
  uploadIds = [],
  params = {},
) {
  const requestParams = { ...params };
  if (uploadIds.length) requestParams.upload_ids = uploadIds.join(',');
  return (
    await http.get(`${prefix(workspaceId, provider, authId)}/videos/uploads`, {
      params: requestParams,
    })
  ).data;
}

export async function waitForWebsiteAdsVideoUploads(
  workspaceId,
  provider,
  authId,
  uploadIds,
  { onProgress, timeoutMs = 30 * 60 * 1000 } = {},
) {
  const ids = [...new Set((uploadIds || []).map(Number).filter(Boolean))];
  if (!ids.length) return { items: [], completed: 0, failed: 0, pending: 0 };
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await listAllWebsiteAdsVideoUploads(workspaceId, provider, authId, ids);
    const items = Array.isArray(response?.items) ? response.items : [];
    const failed = items.filter((item) => item.upload_status === 'FAILED').length;
    const completed = items.filter((item) => ['UPLOADED', 'DUPLICATE'].includes(item.upload_status)).length;
    const pending = Math.max(0, ids.length - completed - failed);
    const state = { items, completed, failed, pending, total: ids.length };
    onProgress?.(state);
    if (items.length === ids.length && pending === 0) return state;
    await new Promise((resolve) => window.setTimeout(resolve, VIDEO_UPLOAD_POLL_INTERVAL_MS));
  }
  throw new Error('后台上传仍在处理中，可稍后在素材库查看最终状态');
}

export async function syncWebsiteAdsCreativeAssets(workspaceId, provider, authId) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/creative-assets/sync`)).data;
}

export async function listWebsiteAdsCreativeAssets(workspaceId, provider, authId, params = {}) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/creative-assets`, { params })).data;
}

export async function updateWebsiteAdsCreativeAsset(workspaceId, provider, authId, assetId, payload) {
  return (await http.patch(`${prefix(workspaceId, provider, authId)}/creative-assets/${encode(assetId)}`, payload)).data;
}

export async function analyzeWebsiteAdsCreativeAsset(workspaceId, provider, authId, assetId) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/creative-assets/${encode(assetId)}/analyze`)).data;
}

export async function generateWebsiteAdsMediaPlan(workspaceId, provider, authId, payload) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/media-plans/generate`, payload)).data;
}

export async function getWebsiteAdsMediaPlanGeneration(workspaceId, provider, authId, planId) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/media-plans/${encode(planId)}/generation`)).data;
}

export async function listWebsiteAdsMediaPlans(workspaceId, provider, authId, params = {}) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/media-plans`, { params })).data;
}

export async function executeWebsiteAdsMediaPlan(workspaceId, provider, authId, planId) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/media-plans/${encode(planId)}/execute`)).data;
}

export async function getWebsiteAdsMediaPlanExecution(workspaceId, provider, authId, planId) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/media-plans/${encode(planId)}/execution`)).data;
}

export async function searchLocations(workspaceId, provider, authId, payload) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/targeting/locations`, payload)).data;
}

export async function searchInterests(workspaceId, provider, authId, payload) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/targeting/interests`, payload)).data;
}

export async function launchWebsiteAd(workspaceId, provider, authId, payload) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/launch`, payload)).data;
}

export async function listWebsiteAdsCampaigns(workspaceId, provider, authId, params = {}) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/campaigns`, { params })).data;
}

function stableFieldKey(field) {
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

function websitePaginationError(label, message, details = {}) {
  const error = new Error(`${label} ${message}`);
  error.code = 'WEBSITE_ADS_PAGINATION_INVARIANT';
  Object.assign(error, details);
  return error;
}

async function listAllTopLevelItems(
  fetchPage,
  label,
  getItemKey,
  { maxPages = 1000 } = {},
) {
  const normalizedMaxPages = Number(maxPages);
  if (!Number.isInteger(normalizedMaxPages) || normalizedMaxPages < 1) {
    throw new Error(`${label} maxPages must be a positive integer`);
  }
  if (typeof getItemKey !== 'function') {
    throw new TypeError(`${label} requires a stable item key`);
  }

  let firstPayload;
  let expectedTotal;
  let expectedPageSize;
  const allItems = [];
  const pageSignatures = new Set();
  const seenItemKeys = new Set();
  for (let page = 1; page <= normalizedMaxPages; page += 1) {
    const payload = await fetchPage(page);
    if (!payload || typeof payload !== 'object' || !Array.isArray(payload.items)) {
      throw websitePaginationError(label, `returned invalid items on page ${page}`, {
        page,
        field: 'items',
      });
    }
    const responsePage = Number(payload.page);
    const responsePageSize = Number(payload.page_size);
    const responseTotal = Number(payload.total);
    if (payload.page == null || !Number.isInteger(responsePage) || responsePage !== page) {
      throw websitePaginationError(
        label,
        `returned page ${String(payload.page)} for requested page ${page}`,
        { page, field: 'page', receivedPage: payload.page },
      );
    }
    if (payload.page_size == null || !Number.isInteger(responsePageSize) || responsePageSize < 1) {
      throw websitePaginationError(label, `returned invalid page_size on page ${page}`, {
        page,
        field: 'page_size',
      });
    }
    if (payload.total == null || !Number.isInteger(responseTotal) || responseTotal < 0) {
      throw websitePaginationError(label, `returned invalid total on page ${page}`, {
        page,
        field: 'total',
      });
    }
    if (expectedPageSize === undefined) {
      expectedPageSize = responsePageSize;
    } else if (responsePageSize !== expectedPageSize) {
      throw websitePaginationError(
        label,
        `page_size changed during pagination (${expectedPageSize} -> ${responsePageSize})`,
        {
          page,
          field: 'page_size',
          expectedPageSize,
          receivedPageSize: responsePageSize,
        },
      );
    }
    if (expectedTotal === undefined) {
      expectedTotal = responseTotal;
    } else if (responseTotal !== expectedTotal) {
      throw websitePaginationError(
        label,
        `total changed during pagination (${expectedTotal} -> ${responseTotal})`,
        {
          page,
          field: 'total',
          expectedTotal,
          receivedTotal: responseTotal,
        },
      );
    }

    const expectedPageItemCount = Math.max(
      0,
      Math.min(
        responsePageSize,
        responseTotal - (responsePage - 1) * responsePageSize,
      ),
    );
    if (payload.items.length !== expectedPageItemCount) {
      throw websitePaginationError(
        label,
        `returned ${payload.items.length} rows on page ${page}; metadata requires ${expectedPageItemCount}`,
        {
          page,
          expectedPageItemCount,
          receivedPageItemCount: payload.items.length,
        },
      );
    }
    const signature = payload.items.length ? JSON.stringify(payload.items) : null;
    if (signature && pageSignatures.has(signature)) {
      throw websitePaginationError(label, `repeated page content on page ${page}`, {
        page,
        reason: 'repeated_page_content',
      });
    }
    if (signature) pageSignatures.add(signature);

    const pageItemKeys = new Set();
    payload.items.forEach((item, itemIndex) => {
      let itemKey;
      try {
        itemKey = getItemKey(item);
      } catch (cause) {
        throw websitePaginationError(
          label,
          `returned an invalid item key on page ${page}, index ${itemIndex}`,
          {
            page,
            itemIndex,
            field: 'item_id',
            cause,
          },
        );
      }
      if (typeof itemKey !== 'string' || itemKey.length === 0) {
        throw websitePaginationError(
          label,
          `returned an item without a stable id on page ${page}, index ${itemIndex}`,
          {
            page,
            itemIndex,
            field: 'id',
            reason: 'missing_item_key',
          },
        );
      }
      if (pageItemKeys.has(itemKey) || seenItemKeys.has(itemKey)) {
        throw websitePaginationError(
          label,
          `returned duplicate item id ${itemKey} on page ${page}`,
          {
            page,
            itemIndex,
            duplicateKey: itemKey,
            reason: 'duplicate_item_key',
            duplicateScope: pageItemKeys.has(itemKey) ? 'within_page' : 'across_pages',
          },
        );
      }
      pageItemKeys.add(itemKey);
    });
    pageItemKeys.forEach((itemKey) => seenItemKeys.add(itemKey));
    allItems.push(...payload.items);
    if (seenItemKeys.size !== allItems.length) {
      throw websitePaginationError(label, 'returned a non-unique item collection', {
        page,
        reason: 'duplicate_item_key',
      });
    }
    if (seenItemKeys.size > expectedTotal) {
      throw websitePaginationError(
        label,
        `returned ${seenItemKeys.size} unique items for total ${expectedTotal}`,
        {
          page,
          expectedTotal,
          collectedItemCount: seenItemKeys.size,
        },
      );
    }
    if (!firstPayload) firstPayload = payload;
    if (seenItemKeys.size === expectedTotal) {
      return {
        ...firstPayload,
        items: allItems,
        page: 1,
        page_size: Number(firstPayload.page_size),
        total: expectedTotal,
      };
    }
    if (payload.items.length === 0) {
      throw websitePaginationError(
        label,
        `returned an empty non-terminal page ${page} (${allItems.length}/${expectedTotal})`,
        { page, expectedTotal, collectedItemCount: seenItemKeys.size },
      );
    }
  }
  throw new Error(
    `${label} exceeded the configured pagination limit of ${normalizedMaxPages} pages`,
  );
}

export async function listAllWebsiteAdsCampaigns(
  workspaceId,
  provider,
  authId,
  params = {},
  options = {},
) {
  const requestedPageSize = Math.min(100, Math.max(1, Number(params.page_size) || 100));
  const baseParams = { ...params, page_size: requestedPageSize };
  delete baseParams.page;
  delete baseParams.limit;
  return listAllTopLevelItems(
    (page) => listWebsiteAdsCampaigns(
      workspaceId,
      provider,
      authId,
      { ...baseParams, page },
    ),
    'Website Ads campaigns',
    stableFieldKey('id'),
    options,
  );
}

export async function listAllWebsiteAdsVideoUploads(
  workspaceId,
  provider,
  authId,
  uploadIds = [],
  params = {},
  options = {},
) {
  const requestedPageSize = Math.min(100, Math.max(1, Number(params.page_size) || 100));
  const baseParams = { ...params, page_size: requestedPageSize };
  delete baseParams.page;
  delete baseParams.limit;
  return listAllTopLevelItems(
    (page) => listWebsiteAdsVideoUploads(
      workspaceId,
      provider,
      authId,
      uploadIds,
      { ...baseParams, page },
    ),
    'Website Ads video uploads',
    stableFieldKey('id'),
    options,
  );
}

export async function listAllWebsiteAdsMediaPlans(
  workspaceId,
  provider,
  authId,
  params = {},
  options = {},
) {
  const requestedPageSize = Math.min(100, Math.max(1, Number(params.page_size) || 100));
  const baseParams = { ...params, page_size: requestedPageSize };
  delete baseParams.page;
  delete baseParams.limit;
  return listAllTopLevelItems(
    (page) => listWebsiteAdsMediaPlans(
      workspaceId,
      provider,
      authId,
      { ...baseParams, page },
    ),
    'Website Ads media plans',
    stableFieldKey('id'),
    options,
  );
}

export async function listWebsiteAdsDailyReports(workspaceId, provider, authId, params = {}) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/reports/daily`, { params })).data;
}

export async function listAllWebsiteAdsDailyReports(
  workspaceId,
  provider,
  authId,
  params = {},
  options = {},
) {
  const requestedPageSize = Math.min(200, Math.max(1, Number(params.page_size) || 200));
  const baseParams = { ...params, page_size: requestedPageSize };
  delete baseParams.page;
  delete baseParams.limit;
  return listAllTopLevelItems(
    (page) => listWebsiteAdsDailyReports(
      workspaceId,
      provider,
      authId,
      { ...baseParams, page },
    ),
    'Website Ads daily reports',
    stableFieldKey('id'),
    options,
  );
}

export async function updateWebsiteAdStatus(workspaceId, provider, authId, adId, operationStatus, reason) {
  return (
    await http.post(`${prefix(workspaceId, provider, authId)}/ads/${encode(adId)}/status`, {
      operation_status: operationStatus,
      reason,
    })
  ).data;
}

export async function updateWebsiteCampaignStatus(workspaceId, provider, authId, campaignId, operationStatus, reason) {
  return (
    await http.post(`${prefix(workspaceId, provider, authId)}/campaigns/${encode(campaignId)}/status`, {
      operation_status: operationStatus,
      reason,
    })
  ).data;
}

export async function updateWebsiteAdGroupDelivery(workspaceId, provider, authId, adGroupId, payload) {
  return (await http.patch(`${prefix(workspaceId, provider, authId)}/adgroups/${encode(adGroupId)}/delivery`, payload)).data;
}

export async function listWebsiteAdsActions(workspaceId, provider, authId, params = {}) {
  return (await http.get(`${prefix(workspaceId, provider, authId)}/actions`, { params })).data;
}

export async function listAllWebsiteAdsActions(
  workspaceId,
  provider,
  authId,
  params = {},
  options = {},
) {
  const requestedPageSize = Math.min(200, Math.max(1, Number(params.page_size) || 200));
  const baseParams = { ...params, page_size: requestedPageSize };
  delete baseParams.page;
  delete baseParams.limit;
  return listAllTopLevelItems(
    (page) => listWebsiteAdsActions(
      workspaceId,
      provider,
      authId,
      { ...baseParams, page },
    ),
    'Website Ads actions',
    stableFieldKey('id'),
    options,
  );
}

export async function runWebsiteAdsMonitor(workspaceId, provider, authId) {
  return (await http.post(`${prefix(workspaceId, provider, authId)}/monitor/run`)).data;
}
