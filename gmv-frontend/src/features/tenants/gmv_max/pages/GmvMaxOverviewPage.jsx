import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useQueries, useQueryClient } from '@tanstack/react-query';

import Modal from '@/components/ui/Modal.jsx';
import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';

import {
  useAccountsQuery,
  useApplyGmvMaxActionMutation,
  useCreateGmvMaxCampaignMutation,
  useGmvMaxCampaignsQuery,
  useGmvMaxConfigQuery,
  useGmvMaxMetricsQuery,
  useGmvMaxOptionsQuery,
  useProductsQuery,
  useSyncGmvMaxCampaignsMutation,
  useUpdateGmvMaxCampaignMutation,
  useUpdateGmvMaxConfigMutation,
  useUpdateGmvMaxStrategyMutation,
} from '../hooks/gmvMaxQueries.js';
import { clampPageSize, getGmvMaxCampaign, getGmvMaxOptions } from '../api/gmvMaxApi.js';
import { loadScope, saveScope } from '../utils/scopeStorage.js';
import {
  MAX_SCOPE_PRESETS,
  buildScopePresetId,
  loadScopePresets,
  saveScopePresets,
} from '../utils/scopePresets.js';

const PROVIDER = 'tiktok-business';
const PROVIDER_LABEL = 'TikTok Business';
const DEFAULT_REPORT_METRICS = [
  'cost',
  'net_cost',
  'orders',
  'cost_per_order',
  'gross_revenue',
  'roi',
];
const EMPTY_QUERY_PARAMS = Object.freeze({});

function formatError(error) {
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

function formatISODate(date) {
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(
    date.getUTCDate(),
  ).padStart(2, '0')}`;
}

function getRecentDateRange(days) {
  const end = new Date();
  const endUtc = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate()));
  const startUtc = new Date(endUtc);
  startUtc.setUTCDate(startUtc.getUTCDate() - (days - 1));
  return {
    start: formatISODate(startUtc),
    end: formatISODate(endUtc),
  };
}

function getProductIdentifier(product) {
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

function getProductStatus(product) {
  if (!product || typeof product !== 'object') return '';
  return (
    product.gmv_max_ads_status ||
    product.status ||
    product.product_status ||
    product.state ||
    ''
  );
}

function isProductAvailable(product) {
  const status = String(getProductStatus(product) || '').trim().toUpperCase();
  if (!status) return true;
  if (status.includes('NOT_AVAILABLE')) return false;
  if (status.includes('UNAVAILABLE')) return false;
  return true;
}

function normalizeIdValue(value) {
  if (value === undefined || value === null) return '';
  const stringValue = String(value).trim();
  return stringValue;
}

function addId(target, value) {
  const normalized = normalizeIdValue(value);
  if (!normalized) return;
  target.add(normalized);
}

function ensureIdSet(target) {
  if (target && typeof target.add === 'function') {
    return target;
  }
  return new Set();
}

function collectBusinessCenterIdsFromCampaign(campaign, target) {
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

function collectBusinessCenterIdsFromDetail(detail, target) {
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

function collectAdvertiserIdsFromCampaign(campaign, target) {
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

function collectAdvertiserIdsFromDetail(detail, target) {
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

function collectStoreIdsFromCampaign(campaign, target) {
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

function collectStoreIdsFromDetail(detail, target) {
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

function addProductIdentifier(target, value) {
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

function collectProductIdsFromList(list, target) {
  const ids = ensureIdSet(target);
  const items = ensureArray(list);
  items.forEach((value) => addProductIdentifier(ids, value));
  return ids;
}

function collectProductIdsFromCampaign(campaign, target) {
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

function collectProductIdsFromDetail(detail, target) {
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

function buildScopeMatchResult(ids, detailIds, detailLoading, target, options) {
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

function matchesBusinessCenter(
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

function matchesAdvertiser(campaign, detail, detailLoading, selectedAdvertiserId, scopeFallback) {
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

function matchesStore(campaign, detail, detailLoading, selectedStoreId, options) {
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

function matchesCampaignScope(card, filters) {
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

const DEFAULT_SCOPE = {
  accountAuthId: null,
  bcId: null,
  advertiserId: null,
  storeId: null,
};

function ensureArray(value) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') {
    if (Array.isArray(value.items)) return value.items;
    if (Array.isArray(value.list)) return value.list;
  }
  return [];
}

function getOptionLabel(options, value) {
  if (!value) return '';
  const normalized = String(value);
  const option = (options || []).find((item) => String(item?.value ?? '') === normalized);
  return option?.label || normalized;
}

function getBusinessCenterId(bc) {
  if (!bc || typeof bc !== 'object') return '';
  return normalizeIdValue(
    bc.bc_id ?? bc.id ?? bc.business_center_id ?? bc.businessCenterId ?? bc.bcId ?? '',
  );
}

function getBusinessCenterLabel(bc) {
  if (!bc || typeof bc !== 'object') return '';
  return bc.name || bc.bc_name || bc.bcName || getBusinessCenterId(bc) || 'Business center';
}

function getAdvertiserId(advertiser) {
  if (!advertiser || typeof advertiser !== 'object') return '';
  return normalizeIdValue(advertiser.advertiser_id ?? advertiser.id ?? advertiser.advertiserId ?? '');
}

function getAdvertiserLabel(advertiser) {
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

function getStoreId(store) {
  if (!store || typeof store !== 'object') return '';
  return normalizeIdValue(store.store_id ?? store.id ?? store.storeId ?? '');
}

function getStoreLabel(store) {
  if (!store || typeof store !== 'object') return '';
  return store.name || store.store_name || store.storeName || getStoreId(store) || 'Store';
}

function normalizeLinksMap(raw) {
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

function extractLinkMap(links, ...candidates) {
  if (!links || typeof links !== 'object') return new Map();
  for (const key of candidates) {
    if (links[key]) {
      return normalizeLinksMap(links[key]);
    }
  }
  return new Map();
}

function normalizeStatusValue(value) {
  if (value === undefined || value === null) return '';
  return String(value).trim().toUpperCase();
}

function filterCampaignsByStatus(campaigns) {
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

function parseOptionalFloat(value) {
  if (value === undefined || value === null || value === '') return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function summariseMetrics(report) {
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

function formatMoney(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatRoi(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toFixed(2);
}

function formatCampaignStatus(status) {
  if (!status) return 'Unknown';
  const map = {
    STATUS_DELIVERY_OK: 'Running',
    STATUS_ENABLE: 'Running',
    STATUS_DISABLE: 'Paused',
    STATUS_ARCHIVED: 'Archived',
  };
  return map[status] || status;
}

const ENABLED_STATUS_WHITELIST = new Set([
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

function isCampaignEnabledStatus(status) {
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

function extractProductsFromDetail(detail) {
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

function setsEqual(a, b) {
  if (a.size !== b.size) return false;
  for (const value of a) {
    if (!b.has(value)) return false;
  }
  return true;
}

function toChoiceList(items) {
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

function extractChoiceList(candidate) {
  if (!candidate) return [];
  if (Array.isArray(candidate)) return toChoiceList(candidate);
  if (Array.isArray(candidate.options)) return toChoiceList(candidate.options);
  if (Array.isArray(candidate.values)) return toChoiceList(candidate.values);
  if (Array.isArray(candidate.items)) return toChoiceList(candidate.items);
  return [];
}

function ErrorBlock({ error, onRetry, message: overrideMessage }) {
  if (!error) return null;
  console.error('GMV Max request failed', error);
  const message = overrideMessage ?? formatError(error) ?? 'Something went wrong. Please try again.';
  const safeMessage =
    typeof message === 'string' && message.trim().startsWith('[')
      ? 'Something went wrong. Please try again.'
      : message;
  return (
    <div className="gmvmax-inline-error" role="alert">
      <span>{safeMessage}</span>
      {onRetry ? (
        <button type="button" onClick={onRetry} className="gmvmax-button gmvmax-button--link">
          Retry
        </button>
      ) : null}
    </div>
  );
}

function SeriesErrorNotice({ error, onRetry }) {
  if (!error) return null;
  console.error('Failed to load GMV Max series', error);
  return (
    <div className="gmvmax-error-card" role="alert">
      <div>
        <h3>Failed to load GMV Max series</h3>
        <p>Please check your filters and try again.</p>
      </div>
      {onRetry ? (
        <button type="button" onClick={onRetry} className="gmvmax-button gmvmax-button--primary">
          Retry
        </button>
      ) : null}
    </div>
  );
}

function ProductSelectionPanel({
  products,
  selectedIds,
  onToggle,
  onToggleAll,
  storeNames,
  loading,
  emptyMessage,
  disabled,
}) {
  const selection = useMemo(() => {
    if (selectedIds instanceof Set) return selectedIds;
    if (Array.isArray(selectedIds)) return new Set(selectedIds.map(String));
    return new Set();
  }, [selectedIds]);

  const productRows = Array.isArray(products) ? products : [];
  const allIds = useMemo(
    () => productRows.map((product) => getProductIdentifier(product)).filter(Boolean),
    [productRows],
  );
  const allSelected = allIds.length > 0 && allIds.every((id) => selection.has(id));

  if (loading) {
    return <Loading text="Loading products…" />;
  }

  if (productRows.length === 0) {
    return <p>{emptyMessage || 'No products available.'}</p>;
  }

  return (
    <div className="gmvmax-product-table">
      <div className="gmvmax-product-table__actions">
        <label>
          <input
            type="checkbox"
            checked={allSelected}
            onChange={() => onToggleAll?.(allIds)}
            disabled={disabled || allIds.length === 0}
          />
          <span>Select all</span>
        </label>
        <span className="gmvmax-product-table__count">
          Selected {selection.size} / {productRows.length}
        </span>
      </div>
      <table className="gmvmax-table">
        <thead>
          <tr>
            <th aria-label="select" />
            <th>Product</th>
            <th>Product ID</th>
            <th>Store</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {productRows.map((product) => {
            const id = getProductIdentifier(product);
            if (!id) return null;
            const checked = selection.has(id);
            const imageUrl =
              product.image_url || product.cover_image || product.thumbnail_url || product.imageUrl || null;
            const storeKey = String(product.store_id ?? product.storeId ?? '');
            const storeLabel = storeKey && storeNames?.get(storeKey) ? storeNames.get(storeKey) : storeKey || '—';
            const status =
              product.gmv_max_ads_status || product.status || product.product_status || product.state || '—';
            return (
              <tr key={id}>
                <td>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => onToggle?.(id)}
                    disabled={disabled}
                  />
                </td>
                <td>
                  <div className="gmvmax-product-cell">
                    {imageUrl ? (
                      <img
                        src={imageUrl}
                        alt={product.title || product.name || id}
                        className="gmvmax-product-thumb"
                        style={{ width: 48, height: 48, objectFit: 'cover', borderRadius: 6 }}
                      />
                    ) : (
                      <span className="gmvmax-product-thumb gmvmax-product-thumb--empty">—</span>
                    )}
                    <span>{product.title || product.name || 'Unnamed product'}</span>
                  </div>
                </td>
                <td>{id}</td>
                <td>{storeLabel}</td>
                <td>{status}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CampaignCard({
  campaign,
  detail,
  detailLoading,
  detailError,
  onRetryDetail,
  workspaceId,
  provider,
  authId,
  storeId,
  onEdit,
  onManage,
  onDashboard,
}) {
  const campaignId = campaign?.campaign_id || campaign?.id;
  const { start, end } = useMemo(() => getRecentDateRange(7), []);
  const queryClient = useQueryClient();
  const campaignsQueryKey = useMemo(
    () => ['gmvMax', 'campaigns', workspaceId, provider, authId],
    [authId, provider, workspaceId],
  );
  const campaignDetailQueryKey = useMemo(
    () =>
      campaignId
        ? ['gmvMax', 'campaign-detail', workspaceId, provider, authId, campaignId]
        : null,
    [authId, campaignId, provider, workspaceId],
  );
  const metricsQuery = useGmvMaxMetricsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    {
      start_date: start,
      end_date: end,
      store_ids: storeId ? [String(storeId)] : undefined,
    },
    {
      enabled: Boolean(workspaceId && authId && campaignId && storeId),
    },
  );
  const actionMutation = useApplyGmvMaxActionMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: campaignsQueryKey, refetchType: 'active' });
      if (campaignDetailQueryKey) {
        queryClient.invalidateQueries({ queryKey: campaignDetailQueryKey, refetchType: 'active' });
      }
      queryClient.refetchQueries({ queryKey: campaignsQueryKey, type: 'active' });
      if (campaignDetailQueryKey) {
        queryClient.refetchQueries({ queryKey: campaignDetailQueryKey, type: 'active' });
      }
    },
  });

  const reportPayload = metricsQuery.data?.report ?? metricsQuery.data?.data ?? metricsQuery.data ?? null;
  const metricsSummary = reportPayload ? summariseMetrics(reportPayload) : null;
  const productCount = detail ? collectProductIdsFromDetail(detail).size : null;
  const statusLabel = formatCampaignStatus(campaign?.operation_status);
  const name = campaign?.campaign_name || campaign?.name || `Campaign ${campaignId}`;
  const previewProducts = useMemo(() => extractProductsFromDetail(detail), [detail]);
  const displayedProducts = previewProducts.slice(0, 6);
  const remainingProducts = Math.max(0, previewProducts.length - displayedProducts.length);
  const isEnabled = isCampaignEnabledStatus(
    campaign?.operation_status || campaign?.status || detail?.campaign?.operation_status || detail?.campaign?.status,
  );
  const actionError = actionMutation.error ? formatError(actionMutation.error) : null;

  const handleEnable = useCallback(() => {
    if (!campaignId) return;
    actionMutation.mutate({ type: 'resume' });
  }, [actionMutation, campaignId]);

  const handleDisable = useCallback(() => {
    if (!campaignId) return;
    actionMutation.mutate({ type: 'pause' });
  }, [actionMutation, campaignId]);

  const handleDelete = useCallback(() => {
    if (!campaignId) return;
    const confirmed = window.confirm('Delete this campaign? This action cannot be undone.');
    if (!confirmed) return;
    actionMutation.mutate({ type: 'delete' });
  }, [actionMutation, campaignId]);

  return (
    <article className="gmvmax-campaign-card">
      <header className="gmvmax-campaign-card__header">
        <div className="gmvmax-campaign-card__title">
          <h3 title={name}>{name}</h3>
          <p className="gmvmax-campaign-card__status">{statusLabel}</p>
        </div>
        <div className="gmvmax-campaign-card__toggles" aria-label="Series controls">
          <button
            type="button"
            className={`gmvmax-toggle-button ${isEnabled ? 'gmvmax-toggle-button--active' : ''}`}
            aria-label="Enable series"
            aria-pressed={isEnabled}
            onClick={handleEnable}
            disabled={isEnabled || actionMutation.isPending}
            title="Enable"
          >
            <span aria-hidden="true">▶</span>
          </button>
          <button
            type="button"
            className={`gmvmax-toggle-button ${!isEnabled ? 'gmvmax-toggle-button--active' : ''}`}
            aria-label="Disable series"
            aria-pressed={!isEnabled}
            onClick={handleDisable}
            disabled={!isEnabled || actionMutation.isPending}
            title="Disable"
          >
            <span aria-hidden="true">⏸</span>
          </button>
        </div>
      </header>
      {actionError ? <p className="gmvmax-campaign-card__action-error">{actionError}</p> : null}
      <div className="gmvmax-campaign-card__body">
        {detailLoading ? <Loading text="Loading campaign details…" /> : null}
        <ErrorBlock error={detailError} onRetry={onRetryDetail} />
        <div className="gmvmax-campaign-card__products">
          <div className="gmvmax-campaign-card__products-count">
            <span>Products</span>
            <strong>{productCount ?? '—'}</strong>
          </div>
          {detailLoading ? (
            <span className="gmvmax-campaign-card__products-placeholder">Loading products…</span>
          ) : displayedProducts.length === 0 ? (
            <span className="gmvmax-campaign-card__products-placeholder">Product preview unavailable.</span>
          ) : (
            <div className="gmvmax-product-thumbnails" aria-label="Products preview">
              {displayedProducts.map((product, index) => {
                const key = product.id || product.name || `product-${index}`;
                return (
                  <div key={key} className="gmvmax-product-thumbnail" title={product.name}>
                    {product.image ? (
                      <img src={product.image} alt={product.name || 'Product'} />
                    ) : (
                      <span aria-hidden="true">📦</span>
                    )}
                  </div>
                );
              })}
              {remainingProducts > 0 ? (
                <span className="gmvmax-product-thumbnail gmvmax-product-thumbnail--more">+{remainingProducts}</span>
              ) : null}
            </div>
          )}
        </div>
        <dl className="gmvmax-campaign-card__stats">
          <div>
            <dt>Spend (7d)</dt>
            <dd>
              {metricsQuery.isLoading
                ? 'Loading…'
                : metricsSummary
                ? formatMoney(metricsSummary.spend)
                : '—'}
            </dd>
          </div>
          <div>
            <dt>GMV (7d)</dt>
            <dd>
              {metricsQuery.isLoading
                ? 'Loading…'
                : metricsSummary
                ? formatMoney(metricsSummary.gmv)
                : '—'}
            </dd>
          </div>
          <div>
            <dt>ROAS (7d)</dt>
            <dd>
              {metricsQuery.isLoading
                ? 'Loading…'
                : metricsSummary && metricsSummary.roas !== null
                ? formatRoi(metricsSummary.roas)
                : '—'}
            </dd>
          </div>
        </dl>
        <ErrorBlock error={metricsQuery.error} onRetry={metricsQuery.refetch} />
      </div>
      <footer className="gmvmax-campaign-card__footer">
        <button
          type="button"
          className="gmvmax-button gmvmax-button--secondary"
          onClick={() => onEdit?.(campaignId)}
          disabled={!detail || detailLoading}
        >
          Edit
        </button>
        <button
          type="button"
          className="gmvmax-button gmvmax-button--ghost"
          onClick={() => onManage?.(campaignId)}
        >
          Manage
        </button>
        <button
          type="button"
          className="gmvmax-button gmvmax-button--ghost"
          onClick={() => onDashboard?.(campaignId)}
        >
          Dashboard
        </button>
        <button
          type="button"
          className="gmvmax-button gmvmax-button--danger"
          onClick={handleDelete}
          disabled={!campaignId || actionMutation.isPending}
        >
          Delete
        </button>
      </footer>
    </article>
  );
}

function CreateSeriesModal({
  open,
  onClose,
  workspaceId,
  provider,
  authId,
  advertiserId,
  storeId,
  products,
  productsLoading,
  storeNameById,
  initialProductIds,
  onCreated,
}) {
  const [step, setStep] = useState(1);
  const [form, setForm] = useState({
    name: '',
    shoppingAdsType: '',
    optimizationGoal: '',
    bidType: '',
    budget: '',
    roasBid: '',
  });
  const [localSelectedIds, setLocalSelectedIds] = useState(new Set());
  const [submitError, setSubmitError] = useState(null);

  const productsById = useMemo(() => {
    const map = new Map();
    (products || []).forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) {
        map.set(id, product);
      }
    });
    return map;
  }, [products]);

  useEffect(() => {
    if (!open) return;
    setStep(1);
    setSubmitError(null);
    setForm({
      name: '',
      shoppingAdsType: '',
      optimizationGoal: '',
      bidType: '',
      budget: '',
      roasBid: '',
    });
    const ids = (initialProductIds || []).map(String);
    setLocalSelectedIds(new Set(ids));
  }, [open, initialProductIds]);

  useEffect(() => {
    if (!open) return;
    const allowed = new Set((products || []).map((product) => getProductIdentifier(product)).filter(Boolean));
    setLocalSelectedIds((prev) => {
      const next = new Set();
      prev.forEach((id) => {
        if (allowed.has(id)) {
          next.add(id);
        }
      });
      return next;
    });
  }, [open, products]);

  const optionsQuery = useGmvMaxOptionsQuery(
    workspaceId,
    provider,
    authId,
    {},
    {
      enabled: Boolean(open && workspaceId && authId),
    },
  );

  const shoppingAdsChoices = useMemo(() => {
    const payload = optionsQuery.data;
    if (!payload) return [];
    const campaignOptions = payload.campaign_options ?? payload.campaign ?? {};
    return extractChoiceList(
      campaignOptions.shopping_ads_types ??
        campaignOptions.shoppingAdsTypes ??
        campaignOptions.shopping_ads_type_options ??
        campaignOptions.shoppingAdsTypeOptions,
    );
  }, [optionsQuery.data]);

  const optimizationGoalChoices = useMemo(() => {
    const payload = optionsQuery.data;
    if (!payload) return [];
    const campaignOptions = payload.campaign_options ?? payload.campaign ?? {};
    return extractChoiceList(
      campaignOptions.optimization_goals ??
        campaignOptions.optimizationGoals ??
        campaignOptions.optimization_goal_options ??
        campaignOptions.optimizationGoalOptions,
    );
  }, [optionsQuery.data]);

  const bidTypeChoices = useMemo(() => {
    const payload = optionsQuery.data;
    if (!payload) return [];
    const campaignOptions = payload.campaign_options ?? payload.campaign ?? {};
    return extractChoiceList(
      campaignOptions.bid_types ??
        campaignOptions.bidTypes ??
        campaignOptions.bid_type_options ??
        campaignOptions.bidTypeOptions,
    );
  }, [optionsQuery.data]);

  const createMutation = useCreateGmvMaxCampaignMutation(workspaceId, provider, authId);

  const selectedProducts = useMemo(() => {
    return Array.from(localSelectedIds)
      .map((id) => productsById.get(id))
      .filter(Boolean);
  }, [localSelectedIds, productsById]);

  const canProceedStep1 = Boolean(form.name.trim() && form.optimizationGoal && form.shoppingAdsType);
  const canProceedStep2 = selectedProducts.length > 0;

  const goNext = useCallback(() => {
    setStep((prev) => Math.min(prev + 1, 3));
  }, []);

  const goBack = useCallback(() => {
    setStep((prev) => Math.max(prev - 1, 1));
  }, []);

  const toggleProduct = useCallback((id) => {
    setLocalSelectedIds((prev) => {
      const next = new Set(prev);
      const key = String(id);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }, []);

  const toggleAll = useCallback((ids) => {
    setLocalSelectedIds((prev) => {
      const next = new Set(prev);
      const normalized = (ids || []).map(String);
      const shouldDeselect = normalized.every((id) => next.has(id));
      if (shouldDeselect) {
        normalized.forEach((id) => next.delete(id));
      } else {
        normalized.forEach((id) => next.add(id));
      }
      return next;
    });
  }, []);

  const handleSubmit = useCallback(async () => {
    if (!canProceedStep2) return;
    const trimmedName = form.name.trim();
    const payload = {
      campaign: {
        campaign_name: trimmedName,
        shopping_ads_type: form.shoppingAdsType || undefined,
        optimization_goal: form.optimizationGoal || undefined,
        bid_type: form.bidType || undefined,
        advertiser_id: advertiserId ? String(advertiserId) : undefined,
        store_id: storeId ? String(storeId) : undefined,
      },
      session: {
        store_id: storeId ? String(storeId) : undefined,
        product_list: Array.from(localSelectedIds).map((id) => ({ spu_id: String(id) })),
      },
    };

    const budgetValue = parseOptionalFloat(form.budget);
    if (budgetValue !== undefined) {
      payload.campaign.budget = budgetValue;
    }
    const roasValue = parseOptionalFloat(form.roasBid);
    if (roasValue !== undefined) {
      payload.campaign.roas_bid = roasValue;
    }

    setSubmitError(null);
    try {
      await createMutation.mutateAsync(payload);
      onCreated?.();
    } catch (error) {
      setSubmitError(formatError(error));
    }
  }, [
    advertiserId,
    canProceedStep2,
    createMutation,
    form.bidType,
    form.budget,
    form.name,
    form.optimizationGoal,
    form.roasBid,
    form.shoppingAdsType,
    localSelectedIds,
    onCreated,
    storeId,
  ]);

  if (!open) return null;

  return (
    <Modal open={open} title="Create GMV Max Series" onClose={onClose}>
      {optionsQuery.isLoading ? <Loading text="Loading options…" /> : null}
      <ErrorBlock error={optionsQuery.error} onRetry={optionsQuery.refetch} />

      {step === 1 ? (
        <div className="gmvmax-modal-step">
          <h3>Series details</h3>
          <FormField label="Series name">
            <input
              type="text"
              value={form.name}
              onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
              placeholder="Enter series name"
            />
          </FormField>
          <FormField label="Shopping ads type">
            {shoppingAdsChoices.length > 0 ? (
              <select
                value={form.shoppingAdsType}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, shoppingAdsType: event.target.value }))
                }
              >
                <option value="">Select type</option>
                {shoppingAdsChoices.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={form.shoppingAdsType}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, shoppingAdsType: event.target.value }))
                }
                placeholder="e.g. PRODUCT"
              />
            )}
          </FormField>
          <FormField label="Optimization goal">
            {optimizationGoalChoices.length > 0 ? (
              <select
                value={form.optimizationGoal}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, optimizationGoal: event.target.value }))
                }
              >
                <option value="">Select goal</option>
                {optimizationGoalChoices.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={form.optimizationGoal}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, optimizationGoal: event.target.value }))
                }
                placeholder="e.g. GMV"
              />
            )}
          </FormField>
          <FormField label="Bid type">
            {bidTypeChoices.length > 0 ? (
              <select
                value={form.bidType}
                onChange={(event) => setForm((prev) => ({ ...prev, bidType: event.target.value }))}
              >
                <option value="">Select bid type</option>
                {bidTypeChoices.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={form.bidType}
                onChange={(event) => setForm((prev) => ({ ...prev, bidType: event.target.value }))}
                placeholder="Bid type"
              />
            )}
          </FormField>
          <div className="gmvmax-modal-grid">
            <FormField label="Budget">
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.budget}
                onChange={(event) => setForm((prev) => ({ ...prev, budget: event.target.value }))}
                placeholder="Optional"
              />
            </FormField>
            <FormField label="ROAS bid">
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.roasBid}
                onChange={(event) => setForm((prev) => ({ ...prev, roasBid: event.target.value }))}
                placeholder="Optional"
              />
            </FormField>
          </div>
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={onClose}>
              Cancel
            </button>
            <button type="button" onClick={goNext} disabled={!canProceedStep1}>
              Next
            </button>
          </div>
        </div>
      ) : null}

      {step === 2 ? (
        <div className="gmvmax-modal-step">
          <h3>Select products</h3>
          <ProductSelectionPanel
            products={products}
            selectedIds={localSelectedIds}
            onToggle={toggleProduct}
            onToggleAll={toggleAll}
            storeNames={storeNameById}
            loading={productsLoading}
            emptyMessage={productsLoading ? 'Loading products…' : 'No products available.'}
          />
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={goBack}>
              Back
            </button>
            <button type="button" onClick={goNext} disabled={!canProceedStep2}>
              Next
            </button>
          </div>
        </div>
      ) : null}

      {step === 3 ? (
        <div className="gmvmax-modal-step">
          <h3>Review</h3>
          <dl className="gmvmax-review-list">
            <div>
              <dt>Series name</dt>
              <dd>{form.name || '—'}</dd>
            </div>
            <div>
              <dt>Shopping ads type</dt>
              <dd>{form.shoppingAdsType || '—'}</dd>
            </div>
            <div>
              <dt>Optimization goal</dt>
              <dd>{form.optimizationGoal || '—'}</dd>
            </div>
            <div>
              <dt>Bid type</dt>
              <dd>{form.bidType || '—'}</dd>
            </div>
            <div>
              <dt>Budget</dt>
              <dd>{form.budget ? formatMoney(parseOptionalFloat(form.budget)) : '—'}</dd>
            </div>
            <div>
              <dt>ROAS bid</dt>
              <dd>{form.roasBid ? formatMoney(parseOptionalFloat(form.roasBid)) : '—'}</dd>
            </div>
            <div>
              <dt>Products selected</dt>
              <dd>{selectedProducts.length}</dd>
            </div>
          </dl>
          <ul className="gmvmax-review-products">
            {selectedProducts.slice(0, 10).map((product) => {
              const id = getProductIdentifier(product);
              return (
                <li key={id}>
                  {product?.title || product?.name || id} ({id})
                </li>
              );
            })}
            {selectedProducts.length > 10 ? (
              <li>…and {selectedProducts.length - 10} more</li>
            ) : null}
          </ul>
          {submitError ? <div className="gmvmax-error">{submitError}</div> : null}
          {createMutation.isPending ? <Loading text="Creating series…" /> : null}
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={goBack} disabled={createMutation.isPending}>
              Back
            </button>
            <button type="button" onClick={handleSubmit} disabled={createMutation.isPending}>
              Create series
            </button>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}

function EditSeriesModal({
  open,
  onClose,
  workspaceId,
  provider,
  authId,
  campaign,
  detail,
  detailLoading,
  detailError,
  onRetryDetail,
  products,
  productsLoading,
  storeId,
  storeNameById,
  onUpdated,
}) {
  const [name, setName] = useState('');
  const [budget, setBudget] = useState('');
  const [roasBid, setRoasBid] = useState('');
  const [localSelectedIds, setLocalSelectedIds] = useState(new Set());
  const [submitError, setSubmitError] = useState(null);

  const campaignId = campaign?.campaign_id || campaign?.id || '';

  const detailProducts = useMemo(() => {
    const sessions = detail?.sessions || detail?.session_list || [];
    const collected = [];
    sessions.forEach((session) => {
      (session?.product_list || session?.products || []).forEach((product) => {
        if (product) {
          collected.push(product);
        }
      });
    });
    return collected;
  }, [detail]);

  const initialProductSet = useMemo(() => {
    const ids = new Set();
    detailProducts.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) ids.add(id);
    });
    return ids;
  }, [detailProducts]);

  const mergedProducts = useMemo(() => {
    const map = new Map();
    (products || []).forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) {
        map.set(id, product);
      }
    });
    detailProducts.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id && !map.has(id)) {
        map.set(id, product);
      }
    });
    return Array.from(map.values());
  }, [detailProducts, products]);

  useEffect(() => {
    if (!open) return;
    if (!detail) return;
    setName(detail.campaign?.campaign_name || '');
    setBudget(
      detail.campaign?.budget !== undefined && detail.campaign?.budget !== null
        ? String(detail.campaign.budget)
        : '',
    );
    setRoasBid(
      detail.campaign?.roas_bid !== undefined && detail.campaign?.roas_bid !== null
        ? String(detail.campaign.roas_bid)
        : '',
    );
    setLocalSelectedIds(new Set(initialProductSet));
    setSubmitError(null);
  }, [detail, initialProductSet, open]);

  useEffect(() => {
    if (!open) return;
    setLocalSelectedIds((prev) => {
      const next = new Set();
      prev.forEach((id) => {
        if (initialProductSet.has(id)) {
          next.add(id);
        }
      });
      return next;
    });
  }, [initialProductSet, open]);

  const toggleProduct = useCallback((id) => {
    setLocalSelectedIds((prev) => {
      const next = new Set(prev);
      const key = String(id);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }, []);

  const toggleAll = useCallback((ids) => {
    setLocalSelectedIds((prev) => {
      const next = new Set(prev);
      const normalized = (ids || []).map(String);
      const shouldDeselect = normalized.every((id) => next.has(id));
      if (shouldDeselect) {
        normalized.forEach((id) => next.delete(id));
      } else {
        normalized.forEach((id) => next.add(id));
      }
      return next;
    });
  }, []);

  const updateCampaignMutation = useUpdateGmvMaxCampaignMutation(workspaceId, provider, authId, campaignId);
  const strategyMutation = useUpdateGmvMaxStrategyMutation(workspaceId, provider, authId, campaignId);

  const productsChanged = useMemo(
    () => !setsEqual(localSelectedIds, initialProductSet),
    [localSelectedIds, initialProductSet],
  );

  const sessionId = detail?.sessions?.[0]?.session_id || detail?.session?.session_id || null;
  const effectiveStoreId = storeId || detail?.campaign?.store_id || null;

  const handleSubmit = useCallback(async () => {
    if (!campaignId) return;
    const trimmedName = name.trim();
    const campaignPatch = {};
    if (trimmedName && trimmedName !== detail?.campaign?.campaign_name) {
      campaignPatch.campaign_name = trimmedName;
    }
    const budgetValue = parseOptionalFloat(budget);
    if (budgetValue !== undefined && budgetValue !== detail?.campaign?.budget) {
      campaignPatch.budget = budgetValue;
    }
    const roasValue = parseOptionalFloat(roasBid);
    if (roasValue !== undefined && roasValue !== detail?.campaign?.roas_bid) {
      campaignPatch.roas_bid = roasValue;
    }

    const tasks = [];
    setSubmitError(null);

    try {
      if (Object.keys(campaignPatch).length > 0) {
        tasks.push(updateCampaignMutation.mutateAsync(campaignPatch));
      }
      if (productsChanged) {
        if (!sessionId) {
          throw new Error('Unable to update products: missing session information.');
        }
        const productList = Array.from(localSelectedIds).map((id) => ({ spu_id: String(id) }));
        const sessionPayload = {
          session_id: sessionId,
          store_id: effectiveStoreId ? String(effectiveStoreId) : undefined,
          product_list: productList,
        };
        tasks.push(strategyMutation.mutateAsync({ session: sessionPayload }));
      }
      if (tasks.length === 0) {
        onClose?.();
        return;
      }
      await Promise.all(tasks);
      onUpdated?.();
      onClose?.();
    } catch (error) {
      setSubmitError(formatError(error));
    }
  }, [
    campaignId,
    detail,
    effectiveStoreId,
    localSelectedIds,
    name,
    budget,
    roasBid,
    onUpdated,
    onClose,
    productsChanged,
    sessionId,
    strategyMutation,
    updateCampaignMutation,
  ]);

  const isSaving = updateCampaignMutation.isPending || strategyMutation.isPending;
  const canSubmit =
    Boolean(detail) &&
    (productsChanged ||
      (name.trim() && name.trim() !== detail?.campaign?.campaign_name) ||
      (budget && parseOptionalFloat(budget) !== detail?.campaign?.budget) ||
      (roasBid && parseOptionalFloat(roasBid) !== detail?.campaign?.roas_bid));

  if (!open) return null;

  return (
    <Modal open={open} title="Edit GMV Max Series" onClose={onClose}>
      {detailLoading ? <Loading text="Loading campaign…" /> : null}
      <ErrorBlock error={detailError} onRetry={onRetryDetail} />
      {!detailLoading && !detailError && !detail ? <p>Campaign details are not available.</p> : null}
      {!detail || detailLoading || detailError ? null : (
        <div className="gmvmax-modal-step">
          <h3>Basic configuration</h3>
          <FormField label="Series name">
            <input type="text" value={name} onChange={(event) => setName(event.target.value)} />
          </FormField>
          <div className="gmvmax-modal-grid">
            <FormField label="Budget">
              <input
                type="number"
                min="0"
                step="0.01"
                value={budget}
                onChange={(event) => setBudget(event.target.value)}
                placeholder="Leave blank to keep current"
              />
            </FormField>
            <FormField label="ROAS bid">
              <input
                type="number"
                min="0"
                step="0.01"
                value={roasBid}
                onChange={(event) => setRoasBid(event.target.value)}
                placeholder="Leave blank to keep current"
              />
            </FormField>
          </div>
          <dl className="gmvmax-review-list">
            <div>
              <dt>Optimization goal</dt>
              <dd>{detail.campaign?.optimization_goal || '—'}</dd>
            </div>
            <div>
              <dt>Shopping ads type</dt>
              <dd>{detail.campaign?.shopping_ads_type || '—'}</dd>
            </div>
          </dl>
          <h3>Products</h3>
          {!sessionId ? (
            <p>Session information is unavailable; product editing is disabled.</p>
          ) : (
            <ProductSelectionPanel
              products={mergedProducts}
              selectedIds={localSelectedIds}
              onToggle={toggleProduct}
              onToggleAll={toggleAll}
              storeNames={storeNameById}
              loading={productsLoading}
              emptyMessage={productsLoading ? 'Loading products…' : 'No products found.'}
              disabled={isSaving}
            />
          )}
          {submitError ? <div className="gmvmax-error">{submitError}</div> : null}
          {isSaving ? <Loading text="Saving changes…" /> : null}
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={onClose} disabled={isSaving}>
              Cancel
            </button>
            <button type="button" onClick={handleSubmit} disabled={isSaving || !canSubmit}>
              Save changes
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

export default function GmvMaxOverviewPage() {
  const { wid: workspaceId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const provider = PROVIDER;
  const [scope, setScope] = useState(() => ({ ...DEFAULT_SCOPE }));
  const [selectedProductIds, setSelectedProductIds] = useState([]);
  const [isCreateModalOpen, setCreateModalOpen] = useState(false);
  const [editingCampaignId, setEditingCampaignId] = useState('');
  const [syncError, setSyncError] = useState(null);
  const [hasLoadedScope, setHasLoadedScope] = useState(false);
  const [scopePresets, setScopePresets] = useState([]);
  const [selectedPresetId, setSelectedPresetId] = useState('');
  const [presetLabelInput, setPresetLabelInput] = useState('');
  const autoOptionsRefreshAccounts = useRef(new Set());

  const authId = scope.accountAuthId ? String(scope.accountAuthId) : '';
  const businessCenterId = scope.bcId ? String(scope.bcId) : '';
  const advertiserId = scope.advertiserId ? String(scope.advertiserId) : '';
  const storeId = scope.storeId ? String(scope.storeId) : '';
  const isScopeReady = Boolean(authId && businessCenterId && advertiserId && storeId);
  const scopeOptionsParams = EMPTY_QUERY_PARAMS;
  const scopeOptionsQueryKey = useMemo(
    () => ['gmvMax', 'options', workspaceId, provider, authId, scopeOptionsParams],
    [authId, provider, scopeOptionsParams, workspaceId],
  );

  useEffect(() => {
    if (!workspaceId) {
      setScope({ ...DEFAULT_SCOPE });
      setHasLoadedScope(false);
      setSelectedPresetId('');
      return;
    }
    const saved = loadScope(workspaceId, provider);
    if (saved) {
      setScope({
        accountAuthId: saved.accountAuthId ?? null,
        bcId: saved.businessCenterId ?? null,
        advertiserId: saved.advertiserId ?? null,
        storeId: saved.storeId ?? null,
      });
    } else {
      setScope({ ...DEFAULT_SCOPE });
    }
    setHasLoadedScope(true);
  }, [provider, workspaceId]);

  useEffect(() => {
    if (!workspaceId) {
      setScopePresets([]);
      setSelectedPresetId('');
      return;
    }
    const presets = loadScopePresets(workspaceId);
    setScopePresets(presets);
    setSelectedPresetId('');
  }, [workspaceId]);

  useEffect(() => {
    if (!workspaceId || !hasLoadedScope) return;
    saveScope(workspaceId, provider, {
      accountAuthId: authId || undefined,
      businessCenterId: businessCenterId || null,
      advertiserId: advertiserId || null,
      storeId: storeId || null,
    });
  }, [
    advertiserId,
    authId,
    businessCenterId,
    hasLoadedScope,
    provider,
    storeId,
    workspaceId,
  ]);

  const accountsQuery = useAccountsQuery(
    workspaceId,
    provider,
    EMPTY_QUERY_PARAMS,
    {
      enabled: Boolean(workspaceId),
    },
  );

  const scopeOptionsQuery = useGmvMaxOptionsQuery(
    workspaceId,
    provider,
    authId,
    scopeOptionsParams,
    {
      enabled: Boolean(workspaceId && authId),
    },
  );

  const bindingConfigQuery = useGmvMaxConfigQuery(
    workspaceId,
    provider,
    authId,
    {
      enabled: Boolean(workspaceId && provider && authId),
    },
  );

  const scopeOptions = scopeOptionsQuery.data || {};
  const scopeOptionsReady = scopeOptionsQuery.isSuccess;

  const businessCenterOptions = useMemo(() => {
    if (!authId) return [];
    const list = ensureArray(
      scopeOptions.bcs ||
        scopeOptions.business_centers ||
        scopeOptions.businessCenters ||
        scopeOptions.bc_list,
    );
    return list
      .map((bc) => {
        const id = getBusinessCenterId(bc);
        if (!id) return null;
        return { value: id, label: getBusinessCenterLabel(bc), data: bc };
      })
      .filter(Boolean);
  }, [authId, scopeOptions]);

  const advertiserList = useMemo(() => {
    return ensureArray(scopeOptions.advertisers || scopeOptions.advertiser_list);
  }, [scopeOptions]);

  const storeList = useMemo(() => {
    return ensureArray(scopeOptions.stores || scopeOptions.store_list);
  }, [scopeOptions]);

  const links = scopeOptions.links || {};
  const bcToAdvertisers = useMemo(
    () => extractLinkMap(links, 'bc_to_advertisers', 'bcToAdvertisers'),
    [links],
  );
  const advertiserToStores = useMemo(
    () => extractLinkMap(links, 'advertiser_to_stores', 'advertiserToStores'),
    [links],
  );

  const advertiserOptions = useMemo(() => {
    if (!authId || !businessCenterId) return [];
    const allowed = bcToAdvertisers.get(businessCenterId);
    const allowedSet = allowed && allowed.length > 0 ? new Set(allowed) : null;
    const hasLinks = bcToAdvertisers.size > 0;
    return advertiserList
      .filter((adv) => {
        const id = getAdvertiserId(adv);
        if (!id) return false;
        if (allowedSet) return allowedSet.has(id);
        return hasLinks ? false : true;
      })
      .map((adv) => ({ value: getAdvertiserId(adv), label: getAdvertiserLabel(adv), data: adv }));
  }, [advertiserList, authId, bcToAdvertisers, businessCenterId]);

  const storeOptions = useMemo(() => {
    if (!authId || !advertiserId) return [];
    const allowed = advertiserToStores.get(advertiserId);
    const allowedSet = allowed && allowed.length > 0 ? new Set(allowed) : null;
    const hasLinks = advertiserToStores.size > 0;
    return storeList
      .filter((store) => {
        const id = getStoreId(store);
        if (!id) return false;
        if (allowedSet) return allowedSet.has(id);
        return hasLinks ? false : true;
      })
      .map((store) => ({ value: getStoreId(store), label: getStoreLabel(store), data: store }));
  }, [advertiserId, advertiserToStores, authId, storeList]);

  useEffect(() => {
    if (!workspaceId || !authId || !scopeOptionsReady) return;
    if (scopeOptionsQuery.isFetching || scopeOptionsQuery.isRefetching) return;
    const hasScopeData =
      businessCenterOptions.length > 0 || advertiserList.length > 0 || storeList.length > 0;
    if (hasScopeData) return;
    const accountKey = `${workspaceId}:${provider}:${authId}`;
    if (autoOptionsRefreshAccounts.current.has(accountKey)) return;
    let cancelled = false;
    let completed = false;
    autoOptionsRefreshAccounts.current.add(accountKey);
    (async () => {
      try {
        const refreshed = await getGmvMaxOptions(workspaceId, provider, authId, { refresh: 1 });
        if (cancelled) return;
        queryClient.setQueryData(scopeOptionsQueryKey, refreshed);
        completed = true;
      } catch (error) {
        console.error('Failed to auto-refresh GMV Max options', error);
        autoOptionsRefreshAccounts.current.delete(accountKey);
      }
    })();
    return () => {
      cancelled = true;
      if (!completed) {
        autoOptionsRefreshAccounts.current.delete(accountKey);
      }
    };
  }, [
    advertiserList.length,
    authId,
    businessCenterOptions.length,
    provider,
    queryClient,
    scopeOptionsQuery.isFetching,
    scopeOptionsQuery.isRefetching,
    scopeOptionsQueryKey,
    scopeOptionsReady,
    storeList.length,
    workspaceId,
  ]);

  const productParams = useMemo(
    () => ({
      store_id: storeId || undefined,
      advertiser_id: advertiserId || undefined,
      owner_bc_id: businessCenterId || undefined,
      page_size: clampPageSize(50),
    }),
    [advertiserId, businessCenterId, storeId],
  );

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    productParams,
    {
      enabled: Boolean(workspaceId && provider && isScopeReady),
    },
  );

  const campaignParams = useMemo(() => {
    const params = { page_size: clampPageSize(50) };
    if (businessCenterId) params.owner_bc_id = businessCenterId;
    if (advertiserId) params.advertiser_id = advertiserId;
    if (storeId) params.store_ids = [String(storeId)];
    return params;
  }, [advertiserId, businessCenterId, storeId]);

  const campaignsQuery = useGmvMaxCampaignsQuery(
    workspaceId,
    provider,
    authId,
    campaignParams,
    {
      enabled: Boolean(workspaceId && provider && isScopeReady),
    },
  );

  const currentScopeKey = useMemo(
    () => ({
      businessCenterId,
      advertiserId,
      storeId,
    }),
    [advertiserId, businessCenterId, storeId],
  );

  const [campaignScopeSnapshot, setCampaignScopeSnapshot] = useState(null);

  useEffect(() => {
    if (campaignsQuery.isSuccess && campaignsQuery.dataUpdatedAt) {
      setCampaignScopeSnapshot(currentScopeKey);
    }
  }, [campaignsQuery.dataUpdatedAt, campaignsQuery.isSuccess, currentScopeKey]);

  useEffect(() => {
    if (!isScopeReady) {
      setCampaignScopeSnapshot(null);
    }
  }, [isScopeReady]);

  const accounts = useMemo(() => {
    const data = accountsQuery.data;
    const items = data?.items || data?.list || data || [];
    return Array.isArray(items) ? items : [];
  }, [accountsQuery.data]);

  const accountOptions = useMemo(
    () =>
      accounts.map((account) => ({
        value: String(account.auth_id ?? account.id ?? ''),
        label: account.label || account.account_name || `Account ${account.auth_id}`,
        status: account.status,
      })),
    [accounts],
  );






  const selectedAccountLabel =
    accountOptions.find((item) => item.value === authId)?.label || '';
  const selectedBusinessCenterLabel =
    businessCenterOptions.find((item) => item.value === businessCenterId)?.label || '';
  const selectedAdvertiserLabel =
    advertiserOptions.find((item) => item.value === advertiserId)?.label || '';
  const selectedStoreLabel = storeOptions.find((item) => item.value === storeId)?.label || '';

  const bindingConfig = bindingConfigQuery.data || null;
  const bindingConfigLoading = bindingConfigQuery.isLoading;
  const bindingConfigFetching = bindingConfigQuery.isFetching;
  const bindingConfigError = bindingConfigQuery.error;
  const savedBusinessCenterId = bindingConfig?.bc_id ? String(bindingConfig.bc_id) : '';
  const savedAdvertiserId = bindingConfig?.advertiser_id ? String(bindingConfig.advertiser_id) : '';
  const savedStoreId = bindingConfig?.store_id ? String(bindingConfig.store_id) : '';
  const savedAutoSyncProducts = Boolean(bindingConfig?.auto_sync_products);
  const hasSavedBinding = Boolean(savedBusinessCenterId && savedAdvertiserId && savedStoreId);
  const scopeMatchesBinding = Boolean(
    hasSavedBinding &&
      businessCenterId &&
      advertiserId &&
      storeId &&
      savedBusinessCenterId === businessCenterId &&
      savedAdvertiserId === advertiserId &&
      savedStoreId === storeId,
  );
  const savedBusinessCenterLabel = getOptionLabel(businessCenterOptions, savedBusinessCenterId);
  const savedAdvertiserLabel = getOptionLabel(advertiserOptions, savedAdvertiserId);
  const savedStoreLabel = getOptionLabel(storeOptions, savedStoreId);
  const savedBindingSummary = useMemo(() => {
    const summaryParts = [
      savedBusinessCenterLabel || savedBusinessCenterId,
      savedAdvertiserLabel || savedAdvertiserId,
      savedStoreLabel || savedStoreId,
    ].filter(Boolean);
    return summaryParts.join(' / ');
  }, [
    savedAdvertiserId,
    savedAdvertiserLabel,
    savedBusinessCenterId,
    savedBusinessCenterLabel,
    savedStoreId,
    savedStoreLabel,
  ]);
  const scopeStatus = useMemo(() => {
    if (!authId) {
      return {
        variant: 'muted',
        message: 'Select an account to configure the GMV Max binding.',
      };
    }
    if (bindingConfigLoading) {
      return { variant: 'muted', message: 'Loading binding configuration…' };
    }
    if (bindingConfigError) {
      return {
        variant: 'error',
        message: `Failed to load binding configuration: ${formatError(bindingConfigError)}`,
      };
    }
    if (!hasSavedBinding) {
      return {
        variant: 'warning',
        message: 'Advertiser not configured. Save the current scope to enable GMV Max syncing.',
      };
    }
    if (scopeMatchesBinding) {
      return {
        variant: 'success',
        message: savedBindingSummary
          ? `Current scope matches saved binding (${savedBindingSummary}).`
          : 'Current scope matches saved binding.',
      };
    }
    return {
      variant: 'warning',
      message: savedBindingSummary
        ? `Saved binding: ${savedBindingSummary}. Save the current scope to update it.`
        : 'Saved binding does not match the selected scope. Save the current scope to update it.',
    };
  }, [
    authId,
    bindingConfigError,
    bindingConfigLoading,
    hasSavedBinding,
    savedBindingSummary,
    scopeMatchesBinding,
  ]);
  const scopeStatusClassName = `gmvmax-status-banner gmvmax-status-banner--${scopeStatus.variant || 'muted'}`;

  const defaultPresetLabel = useMemo(() => {
    const parts = [
      selectedAccountLabel,
      selectedBusinessCenterLabel,
      selectedAdvertiserLabel,
      selectedStoreLabel,
    ].filter(Boolean);
    return parts.join(' / ');
  }, [
    selectedAccountLabel,
    selectedAdvertiserLabel,
    selectedBusinessCenterLabel,
    selectedStoreLabel,
  ]);

  const handleAccountChange = useCallback((event) => {
    const value = event?.target?.value || '';
    setScope({
      accountAuthId: value ? String(value) : null,
      bcId: null,
      advertiserId: null,
      storeId: null,
    });
    setSelectedPresetId('');
  }, []);

  const handleBusinessCenterChange = useCallback((event) => {
    const value = event?.target?.value || '';
    setScope((prev) => ({
      ...prev,
      bcId: value ? String(value) : null,
      advertiserId: null,
      storeId: null,
    }));
    setSelectedPresetId('');
  }, []);

  const handleAdvertiserChange = useCallback((event) => {
    const value = event?.target?.value || '';
    setScope((prev) => ({
      ...prev,
      advertiserId: value ? String(value) : null,
      storeId: null,
    }));
    setSelectedPresetId('');
  }, []);

  const handleStoreChange = useCallback((event) => {
    const value = event?.target?.value || '';
    setScope((prev) => ({
      ...prev,
      storeId: value ? String(value) : null,
    }));
    setSelectedPresetId('');
  }, []);

  const handlePresetChange = useCallback(
    (event) => {
      const presetId = event?.target?.value || '';
      setSelectedPresetId(presetId);
      const preset = scopePresets.find((item) => item.id === presetId);
      if (!preset) return;
      setScope({
        accountAuthId: preset.accountAuthId || null,
        bcId: preset.bcId || null,
        advertiserId: preset.advertiserId || null,
        storeId: preset.storeId || null,
      });
    },
    [scopePresets],
  );

  const handleDeletePreset = useCallback(() => {
    if (!workspaceId || !selectedPresetId) return;
    setScopePresets((prev) => {
      const next = prev.filter((preset) => preset.id !== selectedPresetId);
      saveScopePresets(workspaceId, next);
      return next;
    });
    setSelectedPresetId('');
  }, [selectedPresetId, workspaceId]);

  const handleSavePreset = useCallback(() => {
    if (!workspaceId || !isScopeReady) return;
    const label = presetLabelInput.trim() || defaultPresetLabel || 'GMV Max scope preset';
    const preset = {
      id: buildScopePresetId({
        accountAuthId: authId,
        bcId: businessCenterId,
        advertiserId,
        storeId,
      }),
      label,
      accountAuthId: authId,
      bcId: businessCenterId,
      advertiserId,
      storeId,
    };
    setScopePresets((prev) => {
      const filtered = prev.filter((item) => item.id !== preset.id);
      const next = [preset, ...filtered].slice(0, MAX_SCOPE_PRESETS);
      saveScopePresets(workspaceId, next);
      return next;
    });
    setPresetLabelInput('');
    setSelectedPresetId(preset.id);
  }, [
    advertiserId,
    authId,
    businessCenterId,
    defaultPresetLabel,
    isScopeReady,
    presetLabelInput,
    storeId,
    workspaceId,
  ]);

  useEffect(() => {
    setSelectedProductIds([]);
  }, [advertiserId, authId, businessCenterId, storeId, workspaceId]);

  useEffect(() => {
    if (!authId || !businessCenterId || !scopeOptionsReady) return;
    const hasBusinessCenter = businessCenterOptions.some((option) => option.value === businessCenterId);
    if (hasBusinessCenter) return;
    setScope((prev) => ({
      ...prev,
      bcId: null,
      advertiserId: null,
      storeId: null,
    }));
    setSelectedPresetId('');
  }, [authId, businessCenterId, businessCenterOptions, scopeOptionsReady]);

  useEffect(() => {
    if (!businessCenterId || !advertiserId || !scopeOptionsReady) return;
    const hasAdvertiser = advertiserOptions.some((option) => option.value === advertiserId);
    if (hasAdvertiser) return;
    setScope((prev) => ({
      ...prev,
      advertiserId: null,
      storeId: null,
    }));
    setSelectedPresetId('');
  }, [advertiserId, advertiserOptions, businessCenterId, scopeOptionsReady]);

  useEffect(() => {
    if (!advertiserId || !storeId || !scopeOptionsReady) return;
    const hasStore = storeOptions.some((option) => option.value === storeId);
    if (hasStore) return;
    setScope((prev) => ({
      ...prev,
      storeId: null,
    }));
    setSelectedPresetId('');
  }, [advertiserId, scopeOptionsReady, storeId, storeOptions]);

  useEffect(() => {
    if (!isScopeReady) {
      setSelectedProductIds([]);
    }
  }, [isScopeReady]);

  useEffect(() => {
    setSyncError(null);
  }, [advertiserId, authId, businessCenterId, storeId]);

  useEffect(() => {
    if (hasSavedBinding && scopeMatchesBinding) {
      setSyncError(null);
    }
  }, [hasSavedBinding, scopeMatchesBinding]);

  const storeNameById = useMemo(() => {
    const map = new Map();
    storeOptions.forEach((store) => {
      const id = store.value;
      if (id) {
        map.set(String(id), store.label || String(id));
      }
    });
    return map;
  }, [storeOptions]);

  const products = useMemo(() => {
    if (!isScopeReady) return [];
    const data = productsQuery.data;
    const items = data?.items || data?.list || data || [];
    return Array.isArray(items) ? items : [];
  }, [isScopeReady, productsQuery.data]);

  const campaigns = useMemo(() => {
    if (!isScopeReady) return [];
    const data = campaignsQuery.data;
    const items = data?.items || data?.list || data || [];
    return filterCampaignsByStatus(Array.isArray(items) ? items : []);
  }, [campaignsQuery.data, isScopeReady]);

  const campaignDetailQueries = useQueries({
    queries: isScopeReady
      ? campaigns.map((campaign) => {
          const campaignId = campaign?.campaign_id || campaign?.id;
          return {
            queryKey: [
              'gmvMax',
              'campaign-detail',
              workspaceId,
              provider,
              authId,
              businessCenterId,
              advertiserId,
              storeId,
              campaignId,
            ],
            queryFn: () => getGmvMaxCampaign(workspaceId, provider, authId, campaignId),
            enabled: Boolean(workspaceId && authId && campaignId && isScopeReady),
            staleTime: 60 * 1000,
          };
        })
      : [],
  });

  const campaignDetailsById = useMemo(() => {
    const map = new Map();
    campaigns.forEach((campaign, index) => {
      const campaignId = campaign?.campaign_id || campaign?.id;
      if (!campaignId) return;
      map.set(String(campaignId), campaignDetailQueries[index] || null);
    });
    return map;
  }, [campaignDetailQueries, campaigns]);

  const assignedProductIds = useMemo(() => {
    const ids = new Set();
    campaigns.forEach((campaign) => {
      collectProductIdsFromCampaign(campaign, ids);
    });
    campaignDetailQueries.forEach((result) => {
      const detail = result?.data;
      if (!detail) return;
      collectProductIdsFromDetail(detail, ids);
    });
    return ids;
  }, [campaignDetailQueries, campaigns]);

  const unassignedProducts = useMemo(() => {
    if (!isScopeReady || products.length === 0) return [];
    return products.filter((product) => {
      const id = getProductIdentifier(product);
      if (!id) return false;
      if (!isProductAvailable(product)) return false;
      return !assignedProductIds.has(id);
    });
  }, [assignedProductIds, isScopeReady, products]);

  useEffect(() => {
    setSelectedProductIds((prev) => {
      if (!Array.isArray(prev) || prev.length === 0) return prev;
      const availableIds = new Set(
        unassignedProducts.map((product) => getProductIdentifier(product)).filter(Boolean),
      );
      const filtered = prev.filter((id) => availableIds.has(String(id)));
      return filtered.length === prev.length ? prev : filtered;
    });
  }, [unassignedProducts]);

  const selectedProductIdSet = useMemo(
    () => new Set((selectedProductIds || []).map((id) => String(id))),
    [selectedProductIds],
  );

  const handleToggleProduct = useCallback((id) => {
    setSelectedProductIds((prev) => {
      const next = new Set((prev || []).map((value) => String(value)));
      const key = String(id);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return Array.from(next);
    });
  }, []);

  const handleToggleAllProducts = useCallback(
    (ids) => {
      setSelectedProductIds((prev) => {
        const next = new Set((prev || []).map((value) => String(value)));
        const normalized = (ids || []).map(String);
        const shouldDeselect = normalized.every((id) => next.has(id));
        if (shouldDeselect) {
          normalized.forEach((id) => next.delete(id));
        } else {
          normalized.forEach((id) => next.add(id));
        }
        return Array.from(next);
      });
    },
    [],
  );

  const campaignCards = useMemo(
    () =>
      campaigns.map((campaign) => {
        const campaignId = campaign?.campaign_id || campaign?.id;
        const detailResult = campaignId ? campaignDetailsById.get(String(campaignId)) : null;
        return {
          campaign,
          detail: detailResult?.data,
          detailLoading: detailResult?.isLoading ?? false,
          detailError: detailResult?.error,
          detailRefetch: detailResult?.refetch,
          scopeFallback: campaignScopeSnapshot,
        };
      }),
    [campaignDetailsById, campaignScopeSnapshot, campaigns],
  );

  const filteredCampaignCards = useMemo(() => {
    if (!isScopeReady) return [];
    return campaignCards.filter((card) => {
      const { matches, pending } = matchesCampaignScope(card, {
        businessCenterId,
        advertiserId,
        storeId,
      });
      return matches && !pending;
    });
  }, [advertiserId, businessCenterId, campaignCards, isScopeReady, storeId]);

  const saveBindingMutation = useUpdateGmvMaxConfigMutation(workspaceId, provider, authId, {
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'config', workspaceId, provider, authId] });
    },
  });
  const isSavingBinding = saveBindingMutation.isPending;
  const saveBindingError = saveBindingMutation.error ? formatError(saveBindingMutation.error) : null;
  const canSaveBinding = Boolean(
    isScopeReady &&
      !isSavingBinding &&
      !bindingConfigLoading &&
      !bindingConfigFetching &&
      (!hasSavedBinding || !scopeMatchesBinding),
  );

  const syncMutation = useSyncGmvMaxCampaignsMutation(workspaceId, provider, authId, {
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'campaigns', workspaceId, provider, authId] });
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'products', workspaceId, provider, authId] });
    },
  });

  const lastAutoSyncedScopeRef = useRef(null);

  const canSync = Boolean(
    isScopeReady &&
      hasSavedBinding &&
      scopeMatchesBinding &&
      !isSavingBinding &&
      !bindingConfigLoading &&
      !bindingConfigFetching,
  );
  const canCreateSeries = Boolean(isScopeReady);

  const handleSaveBinding = useCallback(async () => {
    if (!isScopeReady || !businessCenterId || !advertiserId || !storeId) {
      return;
    }
    try {
      await saveBindingMutation.mutateAsync({
        bc_id: String(businessCenterId),
        advertiser_id: String(advertiserId),
        store_id: String(storeId),
        auto_sync_products: savedAutoSyncProducts,
      });
    } catch (error) {
      // handled via mutation state
    }
  }, [
    advertiserId,
    businessCenterId,
    isScopeReady,
    saveBindingMutation,
    savedAutoSyncProducts,
    storeId,
  ]);

  const handleSync = useCallback(async () => {
    if (!isScopeReady) {
      setSyncError('Please select business center, advertiser, and store before syncing GMV Max campaigns.');
      return;
    }
    if (bindingConfigLoading || bindingConfigFetching) {
      setSyncError('Binding configuration is still loading. Please wait before syncing.');
      return;
    }
    if (!hasSavedBinding) {
      setSyncError('Please save the current scope before syncing GMV Max campaigns.');
      return;
    }
    if (!scopeMatchesBinding) {
      setSyncError('The selected scope does not match the saved binding. Save it before syncing.');
      return;
    }
    setSyncError(null);
    const range = getRecentDateRange(7);
    const normalizedBcId = businessCenterId ? String(businessCenterId) : undefined;
    const normalizedStoreId = storeId ? String(storeId) : undefined;
    const payload = {
      owner_bc_id: normalizedBcId,
      bc_id: normalizedBcId,
      advertiser_id: advertiserId ? String(advertiserId) : undefined,
      store_id: normalizedStoreId,
      campaign_filter: normalizedStoreId ? { store_ids: [normalizedStoreId] } : undefined,
      campaign_options: { page_size: clampPageSize(50) },
      report: {
        store_ids: normalizedStoreId ? [normalizedStoreId] : undefined,
        start_date: range.start,
        end_date: range.end,
        metrics: DEFAULT_REPORT_METRICS,
        dimensions: ['campaign_id', 'stat_time_day'],
        enable_total_metrics: true,
      },
    };
    try {
      await syncMutation.mutateAsync(payload);
      await campaignsQuery.refetch();
      await productsQuery.refetch();
    } catch (error) {
      console.error('Failed to sync GMV Max campaigns', error);
      const message = formatError(error);
      setSyncError(
        typeof message === 'string' && message.trim().startsWith('[')
          ? 'Sync failed. Please try again.'
          : message,
      );
    }
  }, [
    advertiserId,
    bindingConfigFetching,
    bindingConfigLoading,
    businessCenterId,
    campaignsQuery,
    hasSavedBinding,
    isScopeReady,
    productsQuery,
    scopeMatchesBinding,
    storeId,
    syncMutation,
  ]);

  useEffect(() => {
    if (!canSync) {
      lastAutoSyncedScopeRef.current = null;
      return;
    }
    if (syncMutation.isPending) return;
    const signature = [
      workspaceId,
      provider,
      authId,
      businessCenterId,
      advertiserId,
      storeId,
    ]
      .map((value) => (value == null ? '' : String(value)))
      .join('|');
    if (signature && lastAutoSyncedScopeRef.current !== signature) {
      lastAutoSyncedScopeRef.current = signature;
      handleSync();
    }
  }, [
    advertiserId,
    authId,
    businessCenterId,
    canSync,
    handleSync,
    provider,
    storeId,
    syncMutation.isPending,
    workspaceId,
  ]);

  const handleOpenCreate = useCallback(() => {
    if (!canCreateSeries) return;
    setCreateModalOpen(true);
  }, [canCreateSeries]);

  const handleCloseCreate = useCallback(() => {
    setCreateModalOpen(false);
  }, []);

  const handleSeriesCreated = useCallback(() => {
    setCreateModalOpen(false);
    setSelectedProductIds([]);
    queryClient.invalidateQueries({ queryKey: ['gmvMax', 'campaigns', workspaceId, provider, authId] });
    queryClient.invalidateQueries({ queryKey: ['gmvMax', 'products', workspaceId, provider, authId] });
    campaignsQuery.refetch();
    productsQuery.refetch();
  }, [authId, campaignsQuery, productsQuery, provider, queryClient, workspaceId]);

  const handleEditRequest = useCallback((campaignId) => {
    setEditingCampaignId(String(campaignId));
  }, []);

  const handleCloseEdit = useCallback(() => {
    setEditingCampaignId('');
  }, []);

  const handleSeriesUpdated = useCallback(() => {
    setEditingCampaignId('');
    queryClient.invalidateQueries({ queryKey: ['gmvMax', 'campaigns', workspaceId, provider, authId] });
    queryClient.invalidateQueries({ queryKey: ['gmvMax', 'products', workspaceId, provider, authId] });
    campaignsQuery.refetch();
    productsQuery.refetch();
  }, [authId, campaignsQuery, productsQuery, provider, queryClient, workspaceId]);

  const buildCampaignSearchParams = useCallback(
    (tab) => {
      const params = new URLSearchParams();
      if (tab) params.set('tab', tab);
      if (provider) params.set('provider', provider);
      if (authId) params.set('authId', authId);
      if (businessCenterId) params.set('businessCenterId', businessCenterId);
      if (advertiserId) params.set('advertiserId', advertiserId);
      if (storeId) params.set('storeId', storeId);
      return params.toString() ? `?${params.toString()}` : '';
    },
    [advertiserId, authId, businessCenterId, provider, storeId],
  );

  const handleManage = useCallback(
    (campaignId) => {
      const search = buildCampaignSearchParams('automation');
      navigate(`/tenants/${workspaceId}/gmvmax/${encodeURIComponent(campaignId)}${search}`);
    },
    [buildCampaignSearchParams, navigate, workspaceId],
  );

  const handleDashboard = useCallback(
    (campaignId) => {
      const search = buildCampaignSearchParams('dashboard');
      navigate(`/tenants/${workspaceId}/gmvmax/${encodeURIComponent(campaignId)}${search}`);
    },
    [buildCampaignSearchParams, navigate, workspaceId],
  );

  const editingDetailResult = useMemo(() => {
    if (!editingCampaignId) return null;
    return campaignDetailsById.get(String(editingCampaignId)) || null;
  }, [campaignDetailsById, editingCampaignId]);

  const editingCampaign = useMemo(
    () => campaigns.find((item) => String(item?.campaign_id ?? item?.id) === String(editingCampaignId)) || null,
    [campaigns, editingCampaignId],
  );

  const editingDetail = editingDetailResult?.data;
  const editingDetailLoading = editingDetailResult?.isLoading ?? false;
  const editingDetailError = editingDetailResult?.error;
  const editingDetailRefetch = editingDetailResult?.refetch;

  const campaignsLoading = Boolean(isScopeReady && (campaignsQuery.isLoading || campaignsQuery.isFetching));
  const productsLoading = Boolean(isScopeReady && (productsQuery.isLoading || productsQuery.isFetching));

  return (
    <div className="gmvmax-page">
      <header className="gmvmax-page__header">
        <div>
          <h1>GMV Max Overview</h1>
          <p className="gmvmax-page__subtitle">
            Monitor TikTok Business performance and manage GMV Max series.
          </p>
        </div>
        <span className="gmvmax-provider-badge">Provider: {PROVIDER_LABEL}</span>
      </header>

      <section className="gmvmax-card gmvmax-card--filters">
        <header className="gmvmax-card__header">
          <div>
            <h2>Scope filters</h2>
            <p>Select the account and store context for GMV Max management.</p>
          </div>
        </header>
        <div className="gmvmax-card__body">
          <div className="gmvmax-field-grid">
            <FormField label="Account">
              <select value={authId} onChange={handleAccountChange}>
                <option value="">Select account</option>
                {accountOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                    {option.status === 'invalid' ? ' (invalid)' : ''}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField label="Business center">
              <select
                value={businessCenterId}
                onChange={handleBusinessCenterChange}
                disabled={!authId || businessCenterOptions.length === 0}
              >
                <option value="">Select business center</option>
                {businessCenterOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField label="Advertiser">
              <select
                value={advertiserId}
                onChange={handleAdvertiserChange}
                disabled={!businessCenterId || advertiserOptions.length === 0}
              >
                <option value="">Select advertiser</option>
                {advertiserOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField label="Store">
              <select
                value={storeId}
                onChange={handleStoreChange}
                disabled={!advertiserId || storeOptions.length === 0}
              >
                <option value="">Select store</option>
                {storeOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </FormField>
          </div>
          <div className="gmvmax-field-grid">
            <FormField label={`Scope presets (max ${MAX_SCOPE_PRESETS})`}>
              <div className="gmvmax-presets-row">
                <select value={selectedPresetId} onChange={handlePresetChange}>
                  <option value="">Select preset</option>
                  {scopePresets.map((preset) => (
                    <option key={preset.id} value={preset.id}>
                      {preset.label}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="gmvmax-button"
                  onClick={handleDeletePreset}
                  disabled={!selectedPresetId}
                >
                  Delete
                </button>
              </div>
            </FormField>
            <FormField label="Save current scope as preset">
              <div className="gmvmax-presets-row">
                <input
                  type="text"
                  value={presetLabelInput}
                  onChange={(event) => setPresetLabelInput(event.target.value)}
                  placeholder={defaultPresetLabel || 'Preset label'}
                  disabled={!isScopeReady}
                />
                <button
                  type="button"
                  className="gmvmax-button"
                  onClick={handleSavePreset}
                  disabled={!isScopeReady}
                >
                  Save preset
                </button>
              </div>
            </FormField>
          </div>
          <div className={scopeStatusClassName}>{scopeStatus.message}</div>
          {saveBindingError ? <p className="gmvmax-inline-error-text">{saveBindingError}</p> : null}
          <div className="gmvmax-card__footer">
            <button
              type="button"
              className="gmvmax-button gmvmax-button--secondary"
              onClick={handleSaveBinding}
              disabled={!canSaveBinding}
            >
              {isSavingBinding ? 'Saving…' : hasSavedBinding ? 'Update binding' : 'Save binding'}
            </button>
            <button
              type="button"
              className="gmvmax-button gmvmax-button--primary"
              onClick={handleSync}
              disabled={!canSync || syncMutation.isPending}
            >
              {syncMutation.isPending ? 'Syncing…' : 'Sync GMV Max Campaigns'}
            </button>
            {syncError ? <p className="gmvmax-inline-error-text">{syncError}</p> : null}
          </div>
        </div>
      </section>

      <section className="gmvmax-card">
        <header className="gmvmax-card__header">
          <div>
            <h2>Unassigned products</h2>
            {selectedAccountLabel ? <p className="gmvmax-subtext">Account: {selectedAccountLabel}</p> : null}
          </div>
          <button
            type="button"
            className="gmvmax-button gmvmax-button--primary"
            onClick={handleOpenCreate}
            disabled={!canCreateSeries || products.length === 0}
          >
            Create GMV Max Series
          </button>
        </header>
        <div className="gmvmax-card__body">
          {!authId ? <p className="gmvmax-placeholder">Select an account to view products.</p> : null}
          {authId && !businessCenterId ? (
            <p className="gmvmax-placeholder">Select a business center to continue.</p>
          ) : null}
          {authId && businessCenterId && !advertiserId ? (
            <p className="gmvmax-placeholder">Select an advertiser to continue.</p>
          ) : null}
          {authId && businessCenterId && advertiserId && !storeId ? (
            <p className="gmvmax-placeholder">Select a store to load products.</p>
          ) : null}
          {isScopeReady ? (
            <>
              <ProductSelectionPanel
                products={unassignedProducts}
                selectedIds={selectedProductIdSet}
                onToggle={handleToggleProduct}
                onToggleAll={handleToggleAllProducts}
                storeNames={storeNameById}
                loading={productsLoading}
                emptyMessage={
                  productsLoading
                    ? 'Loading products…'
                    : 'All products are currently assigned to a GMV Max series.'
                }
              />
              <p className="gmvmax-subtext">
                Selected {selectedProductIdSet.size} product(s) ready for a new GMV Max series.
              </p>
            </>
          ) : null}
          <ErrorBlock
            error={isScopeReady ? productsQuery.error : null}
            onRetry={productsQuery.refetch}
          />
        </div>
      </section>

      <section className="gmvmax-card">
        <header className="gmvmax-card__header">
          <h2>GMV Max series</h2>
        </header>
        <div className="gmvmax-card__body">
          <SeriesErrorNotice
            error={isScopeReady ? campaignsQuery.error : null}
            onRetry={campaignsQuery.refetch}
          />
          {campaignsLoading ? <Loading text="Loading campaigns…" /> : null}
          {!isScopeReady ? (
            <p className="gmvmax-placeholder">Complete the scope filters to load GMV Max series.</p>
          ) : null}
          {isScopeReady &&
          !campaignsLoading &&
          !campaignsQuery.error &&
          filteredCampaignCards.length === 0 ? (
            <p className="gmvmax-placeholder">No GMV Max series found for the selected scope.</p>
          ) : null}
          {isScopeReady ? (
            <div className="gmvmax-campaign-grid">
              {filteredCampaignCards.map(({
                campaign,
                detail,
                detailLoading,
                detailError,
                detailRefetch,
              }) => (
                <CampaignCard
                  key={campaign.campaign_id || campaign.id}
                  campaign={campaign}
                  detail={detail}
                  detailLoading={detailLoading}
                  detailError={detailError}
                  onRetryDetail={detailRefetch}
                  workspaceId={workspaceId}
                  provider={provider}
                  authId={authId}
                  storeId={storeId}
                  onEdit={handleEditRequest}
                  onManage={handleManage}
                  onDashboard={handleDashboard}
                />
              ))}
            </div>
          ) : null}
        </div>
      </section>

      <CreateSeriesModal
        open={isCreateModalOpen}
        onClose={handleCloseCreate}
        workspaceId={workspaceId}
        provider={provider}
        authId={authId}
        advertiserId={advertiserId}
        storeId={storeId}
        products={unassignedProducts}
        productsLoading={productsLoading}
        storeNameById={storeNameById}
        initialProductIds={selectedProductIds}
        onCreated={handleSeriesCreated}
      />

      <EditSeriesModal
        open={Boolean(editingCampaignId)}
        onClose={handleCloseEdit}
        workspaceId={workspaceId}
        provider={provider}
        authId={authId}
        campaign={editingCampaign}
        detail={editingDetail}
        detailLoading={editingDetailLoading}
        detailError={editingDetailError}
        onRetryDetail={editingDetailRefetch}
        products={products}
        productsLoading={productsLoading}
        storeId={storeId}
        storeNameById={storeNameById}
        onUpdated={handleSeriesUpdated}
      />
    </div>
  );
}

