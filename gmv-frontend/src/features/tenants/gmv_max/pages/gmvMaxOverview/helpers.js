export const PROVIDER = 'tiktok-business';
export const PROVIDER_LABEL = 'TikTok Business';
export const DEFAULT_REPORT_METRICS = [
  'cost',
  'net_cost',
  'orders',
  'cost_per_order',
  'gross_revenue',
  'roi',
];
export const EMPTY_QUERY_PARAMS = Object.freeze({});

export function formatMetaSummary(summary) {
  if (!summary || typeof summary !== 'object') return '';
  const describe = (label, item) => {
    if (!item || typeof item !== 'object') return null;
    const added = Number.isFinite(Number(item.added)) ? Number(item.added) : 0;
    const removed = Number.isFinite(Number(item.removed)) ? Number(item.removed) : 0;
    const unchanged = Number.isFinite(Number(item.unchanged)) ? Number(item.unchanged) : 0;
    return `${label} +${added}/-${removed} (unchanged ${unchanged})`;
  };
  const parts = [
    describe('Business centers', summary.bc),
    describe('Advertisers', summary.advertisers),
    describe('Stores', summary.stores),
  ].filter(Boolean);
  return parts.join(' · ');
}

export function formatError(error) {
  if (!error) return null;
  if (typeof error === 'string') return error;
  if (error?.response?.data?.error?.message) return error.response.data.error.message;
  if (error?.response?.data?.message) return error.response.data.message;
  if (error?.response?.data?.detail) {
    const { detail } = error.response.data;
    if (typeof detail === 'string') return detail;
    try {
      return JSON.stringify(detail);
    } catch (serializationError) {
      return String(detail);
    }
  }
  if (error?.message) return error.message;
  return 'Request failed';
}

export function formatISODate(date) {
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(
    date.getUTCDate(),
  ).padStart(2, '0')}`;
}

export function getRecentDateRange(days) {
  const end = new Date();
  const endUtc = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate()));
  const startUtc = new Date(endUtc);
  startUtc.setUTCDate(startUtc.getUTCDate() - (days - 1));
  return {
    start: formatISODate(startUtc),
    end: formatISODate(endUtc),
  };
}

export function getProductIdentifier(product) {
  if (!product) return '';
  const candidates = [
    product.product_id,
    product.productId,
    product.spu_id,
    product.spuId,
    product.item_group_id,
    product.itemGroupId,
    product.item_id,
    product.itemId,
    product.id,
  ];
  for (const candidate of candidates) {
    if (candidate !== undefined && candidate !== null && String(candidate) !== '') {
      return String(candidate);
    }
  }
  return '';
}

export function getProductAvailabilityStatus(product) {
  if (!product || typeof product !== 'object') return '';
  return product.status || product.product_status || product.state || '';
}

export function isProductAvailable(product) {
  const status = String(getProductAvailabilityStatus(product) || '').trim().toUpperCase();
  if (!status) return true;
  if (status.includes('NOT_AVAILABLE')) return false;
  if (status.includes('UNAVAILABLE')) return false;
  return true;
}

export function getAvailableProductIds(products) {
  const ids = new Set();
  (products || []).forEach((product) => {
    if (!isProductAvailable(product)) return;
    const id = getProductIdentifier(product);
    if (id) {
      ids.add(id);
    }
  });
  return ids;
}

export function normalizeIdValue(value) {
  if (value === undefined || value === null) return '';
  const stringValue = String(value).trim();
  return stringValue;
}

export function shouldFetchGmvMaxSeries(options = {}) {
  const {
    workspaceId,
    provider,
    authId,
    isScopeReady,
    hasSavedBinding,
    scopeMatchesBinding,
    bindingConfigLoading,
    bindingConfigFetching,
  } = options;

  if (!workspaceId || !provider || !authId || !isScopeReady) {
    return false;
  }
  if (bindingConfigLoading || bindingConfigFetching) {
    return false;
  }
  if (!hasSavedBinding) {
    return false;
  }
  if (!scopeMatchesBinding) {
    return false;
  }
  return true;
}

export function addId(target, value) {
  const normalized = normalizeIdValue(value);
  if (!normalized) return;
  target.add(normalized);
}

export function ensureIdSet(target) {
  if (target && typeof target.add === 'function') {
    return target;
  }
  return new Set();
}

export function collectBusinessCenterIdsFromCampaign(campaign, target) {
  const ids = ensureIdSet(target);
  if (!campaign || typeof campaign !== 'object') return ids;
  addId(ids, campaign.owner_bc_id);
  addId(ids, campaign.ownerBcId);
  addId(ids, campaign.business_center_id);
  addId(ids, campaign.businessCenterId);
  addId(ids, campaign.bc_id);

  const bcList = campaign.business_center_ids || campaign.businessCenterIds;
  if (Array.isArray(bcList)) {
    bcList.forEach((item) => {
      if (item && typeof item === 'object') {
        addId(ids, item.bc_id);
        addId(ids, item.id);
        addId(ids, item.business_center_id);
        addId(ids, item.businessCenterId);
      } else {
        addId(ids, item);
      }
    });
  }

  const bcObject = campaign.business_center || campaign.businessCenter;
  if (bcObject && typeof bcObject === 'object') {
    addId(ids, bcObject.bc_id);
    addId(ids, bcObject.id);
    addId(ids, bcObject.business_center_id);
    addId(ids, bcObject.businessCenterId);
  }

  const nested = campaign.campaign;
  if (nested && nested !== campaign) {
    collectBusinessCenterIdsFromCampaign(nested, ids);
  }

  return ids;
}

export function collectBusinessCenterIdsFromDetail(detail, target) {
  const ids = collectBusinessCenterIdsFromCampaign(detail?.campaign, target);
  if (!detail || typeof detail !== 'object') return ids;
  const bcObject = detail.business_center || detail.businessCenter;
  if (bcObject && typeof bcObject === 'object') {
    addId(ids, bcObject.bc_id);
    addId(ids, bcObject.id);
    addId(ids, bcObject.business_center_id);
    addId(ids, bcObject.businessCenterId);
  }
  return ids;
}

export function collectAdvertiserIdsFromCampaign(campaign, target) {
  const ids = ensureIdSet(target);
  if (!campaign || typeof campaign !== 'object') return ids;
  addId(ids, campaign.advertiser_id);
  addId(ids, campaign.advertiserId);

  const advertiserObject = campaign.advertiser || campaign.advertiser_info || campaign.advertiserInfo;
  if (advertiserObject && typeof advertiserObject === 'object') {
    addId(ids, advertiserObject.advertiser_id);
    addId(ids, advertiserObject.advertiserId);
    addId(ids, advertiserObject.id);
  }

  const nested = campaign.campaign;
  if (nested && nested !== campaign) {
    collectAdvertiserIdsFromCampaign(nested, ids);
  }

  return ids;
}

export function collectAdvertiserIdsFromDetail(detail, target) {
  const ids = collectAdvertiserIdsFromCampaign(detail?.campaign, target);
  if (!detail || typeof detail !== 'object') return ids;
  const advertiserObject = detail.advertiser || detail.advertiser_info || detail.advertiserInfo;
  if (advertiserObject && typeof advertiserObject === 'object') {
    addId(ids, advertiserObject.advertiser_id);
    addId(ids, advertiserObject.advertiserId);
    addId(ids, advertiserObject.id);
  }
  return ids;
}

export function collectStoreIdsFromCampaign(campaign, target) {
  const ids = ensureIdSet(target);
  if (!campaign || typeof campaign !== 'object') return ids;
  addId(ids, campaign.store_id);
  addId(ids, campaign.storeId);

  const storeObject = campaign.store || campaign.store_info || campaign.storeInfo;
  if (storeObject && typeof storeObject === 'object') {
    addId(ids, storeObject.store_id);
    addId(ids, storeObject.storeId);
    addId(ids, storeObject.id);
  }

  const storeLists = [
    campaign.store_ids,
    campaign.storeIds,
    campaign.stores,
    campaign.store_list,
    campaign.storeList,
  ];
  storeLists.forEach((list) => {
    if (!Array.isArray(list)) return;
    list.forEach((item) => {
      if (item && typeof item === 'object') {
        addId(ids, item.store_id);
        addId(ids, item.storeId);
        addId(ids, item.id);
      } else {
        addId(ids, item);
      }
    });
  });

  const nested = campaign.campaign;
  if (nested && nested !== campaign) {
    collectStoreIdsFromCampaign(nested, ids);
  }

  return ids;
}

export function collectStoreIdsFromDetail(detail, target) {
  const ids = collectStoreIdsFromCampaign(detail?.campaign, target);
  if (!detail || typeof detail !== 'object') return ids;
  const sessions = detail.sessions || detail.session_list || [];
  sessions.forEach((session) => {
    if (!session || typeof session !== 'object') return;
    addId(ids, session.store_id);
    addId(ids, session.storeId);
    const storeObject = session.store || session.store_info || session.storeInfo;
    if (storeObject && typeof storeObject === 'object') {
      addId(ids, storeObject.store_id);
      addId(ids, storeObject.storeId);
      addId(ids, storeObject.id);
    }
    const products = session.product_list || session.products || [];
    products.forEach((product) => {
      if (!product || typeof product !== 'object') return;
      addId(ids, product.store_id);
      addId(ids, product.storeId);
    });
  });
  return ids;
}

export function addProductIdentifier(target, value) {
  let identifier = '';
  if (value && typeof value === 'object') {
    identifier =
      getProductIdentifier(value) ||
      normalizeIdValue(
        value.item_group_id ??
          value.itemGroupId ??
          value.productId ??
          value.itemId ??
          value.spuId ??
          value.group_id ??
          value.groupId ??
          '',
      );
  } else {
    identifier = normalizeIdValue(value);
  }
  if (identifier) {
    target.add(identifier);
  }
}

export function collectProductIdsFromList(list, target) {
  const ids = ensureIdSet(target);
  const items = ensureArray(list);
  items.forEach((value) => addProductIdentifier(ids, value));
  return ids;
}

export function collectProductIdsFromCampaign(campaign, target) {
  const ids = ensureIdSet(target);
  if (!campaign || typeof campaign !== 'object') return ids;

  collectProductIdsFromList(campaign.item_group_ids, ids);
  collectProductIdsFromList(campaign.itemGroupIds, ids);
  collectProductIdsFromList(campaign.item_groups, ids);
  collectProductIdsFromList(campaign.itemGroupList, ids);
  collectProductIdsFromList(campaign.item_group_list, ids);
  collectProductIdsFromList(campaign.item_list, ids);
  collectProductIdsFromList(campaign.itemList, ids);
  collectProductIdsFromList(campaign.item_ids, ids);
  collectProductIdsFromList(campaign.itemIds, ids);
  collectProductIdsFromList(campaign.product_ids, ids);
  collectProductIdsFromList(campaign.productIds, ids);
  collectProductIdsFromList(campaign.product_list, ids);
  collectProductIdsFromList(campaign.productList, ids);
  collectProductIdsFromList(campaign.products, ids);

  const nested = campaign.campaign;
  if (nested && nested !== campaign) {
    collectProductIdsFromCampaign(nested, ids);
  }

  return ids;
}

export function collectProductIdsFromDetail(detail, target) {
  const ids = collectProductIdsFromCampaign(detail?.campaign, target);
  if (!detail || typeof detail !== 'object') return ids;

  collectProductIdsFromList(detail.item_group_ids, ids);
  collectProductIdsFromList(detail.itemGroupIds, ids);
  collectProductIdsFromList(detail.item_groups, ids);
  collectProductIdsFromList(detail.itemGroupList, ids);
  collectProductIdsFromList(detail.item_group_list, ids);
  collectProductIdsFromList(detail.item_list, ids);
  collectProductIdsFromList(detail.itemList, ids);
  collectProductIdsFromList(detail.item_ids, ids);
  collectProductIdsFromList(detail.itemIds, ids);
  collectProductIdsFromList(detail.product_ids, ids);
  collectProductIdsFromList(detail.productIds, ids);
  collectProductIdsFromList(detail.product_list, ids);
  collectProductIdsFromList(detail.productList, ids);
  collectProductIdsFromList(detail.products, ids);

  const sessions = ensureArray(detail.sessions || detail.session_list);
  sessions.forEach((session) => {
    if (!session || typeof session !== 'object') return;
    collectProductIdsFromList(session.product_list || session.products, ids);
    collectProductIdsFromList(session.item_group_ids || session.itemGroupIds, ids);
    collectProductIdsFromList(session.items, ids);
  });

  return ids;
}

export function buildScopeMatchResult(ids, detailIds, detailLoading, target, options) {
  let assumeMatchWhenUnknown = false;
  let fallbackTarget;

  if (options && typeof options === 'object') {
    assumeMatchWhenUnknown = Boolean(options.assumeMatchWhenUnknown);
    fallbackTarget = options.fallbackTarget;
  } else {
    fallbackTarget = options;
  }

  if (!target) {
    return { matches: true, pending: false };
  }

  if (ids.has(target) || detailIds.has(target)) {
    return { matches: true, pending: false };
  }

  const hasAnyIds = ids.size > 0 || detailIds.size > 0;
  if (hasAnyIds) {
    return { matches: false, pending: false };
  }

  if (assumeMatchWhenUnknown) {
    return { matches: true, pending: false };
  }

  if (detailLoading) {
    return { matches: false, pending: true };
  }

  const fallback = normalizeIdValue(fallbackTarget);
  if (fallback && fallback === target) {
    return { matches: true, pending: false };
  }

  return { matches: false, pending: false };
}

export function matchesBusinessCenter(
  campaign,
  detail,
  detailLoading,
  selectedBusinessCenterId,
  scopeFallback,
) {
  if (!selectedBusinessCenterId) {
    return { matches: true, pending: false };
  }
  const target = normalizeIdValue(selectedBusinessCenterId);
  if (!target) {
    return { matches: true, pending: false };
  }
  const ids = collectBusinessCenterIdsFromCampaign(campaign);
  const detailIds = collectBusinessCenterIdsFromDetail(detail);
  const fallback = normalizeIdValue(scopeFallback?.businessCenterId);
  return buildScopeMatchResult(ids, detailIds, detailLoading, target, fallback);
}

export function matchesAdvertiser(campaign, detail, detailLoading, selectedAdvertiserId, scopeFallback) {
  if (!selectedAdvertiserId) {
    return { matches: true, pending: false };
  }
  const target = normalizeIdValue(selectedAdvertiserId);
  if (!target) {
    return { matches: true, pending: false };
  }
  const ids = collectAdvertiserIdsFromCampaign(campaign);
  const detailIds = collectAdvertiserIdsFromDetail(detail);
  const fallback = normalizeIdValue(scopeFallback?.advertiserId);
  return buildScopeMatchResult(ids, detailIds, detailLoading, target, fallback);
}

export function matchesStore(campaign, detail, detailLoading, selectedStoreId, options) {
  if (!selectedStoreId) {
    return { matches: true, pending: false };
  }
  const target = normalizeIdValue(selectedStoreId);
  if (!target) {
    return { matches: true, pending: false };
  }
  const ids = collectStoreIdsFromCampaign(campaign);
  const detailIds = collectStoreIdsFromDetail(detail);
  return buildScopeMatchResult(ids, detailIds, detailLoading, target, options);
}

export function matchesCampaignScope(card, filters) {
  if (!card || !card.campaign) {
    return { matches: false, pending: false };
  }
  const { campaign, detail, detailLoading, scopeFallback } = card;
  const { businessCenterId, advertiserId, storeId } = filters;
  const results = [
    matchesBusinessCenter(campaign, detail, detailLoading, businessCenterId, scopeFallback),
    matchesAdvertiser(campaign, detail, detailLoading, advertiserId, scopeFallback),
    matchesStore(campaign, detail, detailLoading, storeId, {
      assumeMatchWhenUnknown: Boolean(storeId),
    }),
  ];

  return {
    matches: results.every((result) => result.matches),
    pending: results.some((result) => result.pending),
  };
}

export const DEFAULT_SCOPE = {
  accountAuthId: null,
  bcId: null,
  advertiserId: null,
  storeId: null,
};

export function ensureArray(value) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') {
    if (Array.isArray(value.items)) return value.items;
    if (Array.isArray(value.list)) return value.list;
  }
  return [];
}

export function getOptionLabel(options, value) {
  if (!value) return '';
  const normalized = String(value);
  const option = (options || []).find((item) => String(item?.value ?? '') === normalized);
  return option?.label || normalized;
}

export function getBusinessCenterId(bc) {
  if (!bc || typeof bc !== 'object') return '';
  return normalizeIdValue(
    bc.bc_id ?? bc.id ?? bc.business_center_id ?? bc.businessCenterId ?? bc.bcId ?? '',
  );
}

export function getBusinessCenterLabel(bc) {
  if (!bc || typeof bc !== 'object') return '';
  return bc.name || bc.bc_name || bc.bcName || getBusinessCenterId(bc) || 'Business center';
}

export function getAdvertiserBusinessCenterId(advertiser) {
  if (!advertiser || typeof advertiser !== 'object') return '';
  const candidates = [
    advertiser.bc_id,
    advertiser.owner_bc_id,
    advertiser.business_center_id,
    advertiser.bcId,
    advertiser.ownerBcId,
    advertiser.businessCenterId,
  ];
  for (const candidate of candidates) {
    const normalized = normalizeIdValue(candidate);
    if (normalized) {
      return normalized;
    }
  }
  return '';
}

export function collectStoreBusinessCenterCandidates(store) {
  if (!store || typeof store !== 'object') return [];
  const candidates = [
    store.bc_id,
    store.store_authorized_bc_id,
    store.authorized_bc_id,
    store.bc_id_hint,
    store.bcId,
    store.storeAuthorizedBcId,
    store.authorizedBcId,
    store.bcIdHint,
  ];
  return candidates.map((value) => normalizeIdValue(value)).filter(Boolean);
}

export function getAdvertiserId(advertiser) {
  if (!advertiser || typeof advertiser !== 'object') return '';
  return normalizeIdValue(advertiser.advertiser_id ?? advertiser.id ?? advertiser.advertiserId ?? '');
}

export function getAdvertiserLabel(advertiser) {
  if (!advertiser || typeof advertiser !== 'object') return '';
  return (
    advertiser.display_name ||
    advertiser.name ||
    advertiser.advertiser_name ||
    advertiser.advertiserName ||
    getAdvertiserId(advertiser) ||
    'Advertiser'
  );
}

export function getStoreId(store) {
  if (!store || typeof store !== 'object') return '';
  return normalizeIdValue(store.store_id ?? store.id ?? store.storeId ?? '');
}

export function getStoreAdvertiserId(store) {
  if (!store || typeof store !== 'object') return '';
  const candidates = [
    store.advertiser_id,
    store.owner_advertiser_id,
    store.advertiserId,
    store.ownerAdvertiserId,
  ];
  for (const candidate of candidates) {
    const normalized = normalizeIdValue(candidate);
    if (normalized) return normalized;
  }
  return '';
}

export function getStoreLabel(store) {
  if (!store || typeof store !== 'object') return '';
  return store.name || store.store_name || store.storeName || getStoreId(store) || 'Store';
}

export function normalizeLinksMap(raw) {
  const map = new Map();
  if (!raw || typeof raw !== 'object') return map;
  Object.entries(raw).forEach(([key, value]) => {
    const normalizedKey = normalizeIdValue(key);
    if (!normalizedKey) return;
    if (!Array.isArray(value)) return;
    const ids = value.map((item) => normalizeIdValue(item)).filter(Boolean);
    if (ids.length > 0) {
      map.set(normalizedKey, ids);
    }
  });
  return map;
}

export function extractLinkMap(links, ...candidates) {
  if (!links || typeof links !== 'object') return new Map();
  for (const key of candidates) {
    if (links[key]) {
      return normalizeLinksMap(links[key]);
    }
  }
  return new Map();
}

export function normalizeStatusValue(value) {
  if (value === undefined || value === null) return '';
  return String(value).trim().toUpperCase();
}

export function filterCampaignsByStatus(campaigns) {
  if (!Array.isArray(campaigns)) return [];
  return campaigns.filter((campaign) => {
    const operationStatus = normalizeStatusValue(
      campaign?.operation_status ?? campaign?.operationStatus,
    );
    if (operationStatus === 'DELETE') {
      return false;
    }
    const secondaryStatus = normalizeStatusValue(
      campaign?.secondary_status ?? campaign?.secondaryStatus,
    );
    return secondaryStatus !== 'CAMPAIGN_STATUS_DELETE';
  });
}

export function parseOptionalFloat(value) {
  if (value === undefined || value === null || value === '') return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

export function summariseMetrics(report) {
  const entries = Array.isArray(report?.list) ? report.list : [];
  const totals = entries.reduce(
    (acc, entry) => {
      const metrics = entry?.metrics || entry || {};
      const spend = parseFloat(
        metrics.spend ?? metrics.total_spend ?? metrics.totalSpend ?? metrics.total_spend_amount ?? '0',
      );
      if (!Number.isNaN(spend)) {
        acc.spend += spend;
      }
      const revenue = parseFloat(
        metrics.gross_revenue ??
          metrics.gmv ??
          metrics.total_gmv ??
          metrics.total_gross_revenue ??
          '0',
      );
      if (!Number.isNaN(revenue)) {
        acc.gmv += revenue;
      }
      const orders = parseFloat(metrics.orders ?? metrics.total_orders ?? '0');
      if (!Number.isNaN(orders)) {
        acc.orders += orders;
      }
      return acc;
    },
    { spend: 0, gmv: 0, orders: 0 },
  );
  const roas = totals.spend > 0 ? totals.gmv / totals.spend : null;
  return { ...totals, roas };
}

export function formatMoney(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatRoi(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toFixed(2);
}

export function formatCampaignStatus(status) {
  if (!status) return 'Unknown';
  const map = {
    STATUS_DELIVERY_OK: 'Running',
    STATUS_ENABLE: 'Running',
    STATUS_DISABLE: 'Paused',
    STATUS_ARCHIVED: 'Archived',
  };
  return map[status] || status;
}

export const ENABLED_STATUS_WHITELIST = new Set([
  'STATUS_DELIVERY_OK',
  'STATUS_ENABLE',
  'STATUS_ENABLED',
  'STATUS_RUNNING',
  'STATUS_RUN',
  'STATUS_ACTIVE',
  'CAMPAIGN_STATUS_ENABLE',
  'CAMPAIGN_STATUS_ENABLED',
  'CAMPAIGN_STATUS_RUNNING',
]);

export function isCampaignEnabledStatus(status) {
  if (!status) return false;
  const normalized = String(status).toUpperCase();
  if (ENABLED_STATUS_WHITELIST.has(normalized)) return true;
  if (normalized.includes('DISABLE') || normalized.includes('PAUSE') || normalized.includes('ARCHIVE')) {
    return false;
  }
  if (normalized.includes('ENABLE') || normalized.includes('RUN') || normalized.includes('ACTIVE')) {
    return true;
  }
  return false;
}

export function extractProductsFromDetail(detail) {
  if (!detail) return [];
  const products = [];
  const seen = new Set();

  const pushProduct = (item) => {
    if (!item || typeof item !== 'object') return;
    const id = getProductIdentifier(item);
    const name =
      item.product_name ||
      item.productName ||
      item.title ||
      item.name ||
      item.item_name ||
      item.itemName ||
      id ||
      'Product';
    const image =
      item.image_url ||
      item.imageUrl ||
      item.cover_url ||
      item.coverUrl ||
      item.thumbnail_url ||
      item.thumbnailUrl ||
      item.thumb_url ||
      item.thumbUrl ||
      item.main_image ||
      item.mainImage ||
      null;
    const key = id || name || `product-${products.length}`;
    if (seen.has(key)) return;
    seen.add(key);
    products.push({ id: key, name: name || 'Product', image });
  };

  const ingest = (list) => {
    ensureArray(list).forEach((product) => {
      if (Array.isArray(product)) {
        product.forEach((entry) => pushProduct(entry));
      } else {
        pushProduct(product);
      }
    });
  };

  ingest(detail.products);
  ingest(detail.product_list);
  ingest(detail.productList);
  ingest(detail.item_list);
  ingest(detail.itemList);
  ingest(detail.items);
  ingest(detail.campaign?.product_list);
  ingest(detail.campaign?.productList);

  const sessions = ensureArray(
    detail.sessions || detail.session_list || detail.sessionList || detail.session || detail.sessionInfo,
  );
  sessions.forEach((session) => {
    ingest(session?.product_list);
    ingest(session?.productList);
    ingest(session?.products);
    ingest(session?.items);
  });

  return products;
}

export function setsEqual(a, b) {
  if (a.size !== b.size) return false;
  for (const value of a) {
    if (!b.has(value)) return false;
  }
  return true;
}

export function toChoiceList(items) {
  return (items || [])
    .map((item) => {
      if (item === null || item === undefined) return null;
      if (typeof item === 'string' || typeof item === 'number') {
        const value = String(item);
        return { value, label: value };
      }
      if (typeof item === 'object') {
        const value =
          item.value ?? item.key ?? item.id ?? item.code ?? item.slug ?? item.name ?? item.label ?? null;
        if (value === null || value === undefined || String(value) === '') {
          return null;
        }
        const label = item.label ?? item.name ?? item.title ?? String(value);
        return { value: String(value), label };
      }
      return null;
    })
    .filter(Boolean);
}

export function extractChoiceList(candidate) {
  if (!candidate) return [];
  if (Array.isArray(candidate)) return toChoiceList(candidate);
  if (Array.isArray(candidate.options)) return toChoiceList(candidate.options);
  if (Array.isArray(candidate.values)) return toChoiceList(candidate.values);
  if (Array.isArray(candidate.items)) return toChoiceList(candidate.items);
  return [];
}
