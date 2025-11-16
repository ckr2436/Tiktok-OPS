import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useQueries, useQueryClient } from '@tanstack/react-query';

import Modal from '@/components/ui/Modal.jsx';
import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';

import {
  useAccountsQuery,
  useAdvertisersQuery,
  useBusinessCentersQuery,
  useCreateGmvMaxCampaignMutation,
  useGmvMaxCampaignsQuery,
  useGmvMaxMetricsQuery,
  useGmvMaxOptionsQuery,
  useProductsQuery,
  useStoresQuery,
  useSyncGmvMaxCampaignsMutation,
  useUpdateGmvMaxCampaignMutation,
  useUpdateGmvMaxStrategyMutation,
} from '../hooks/gmvMaxQueries.js';
import { clampPageSize, getGmvMaxCampaign } from '../api/gmvMaxApi.js';
import { loadScope, saveScope } from '../utils/scopeStorage.js';

const PROVIDER = 'tiktok-business';
const PROVIDER_LABEL = 'TikTok Business';

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
  const candidates = [product.product_id, product.spu_id, product.item_id, product.id];
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

function collectBusinessCenterIdsFromCampaign(campaign, target = new Set()) {
  if (!campaign || typeof campaign !== 'object') return target;
  addId(target, campaign.owner_bc_id);
  addId(target, campaign.ownerBcId);
  addId(target, campaign.business_center_id);
  addId(target, campaign.businessCenterId);
  addId(target, campaign.bc_id);

  const bcList = campaign.business_center_ids || campaign.businessCenterIds;
  if (Array.isArray(bcList)) {
    bcList.forEach((item) => {
      if (item && typeof item === 'object') {
        addId(target, item.bc_id);
        addId(target, item.id);
        addId(target, item.business_center_id);
        addId(target, item.businessCenterId);
      } else {
        addId(target, item);
      }
    });
  }

  const bcObject = campaign.business_center || campaign.businessCenter;
  if (bcObject && typeof bcObject === 'object') {
    addId(target, bcObject.bc_id);
    addId(target, bcObject.id);
    addId(target, bcObject.business_center_id);
    addId(target, bcObject.businessCenterId);
  }

  const nested = campaign.campaign;
  if (nested && nested !== campaign) {
    collectBusinessCenterIdsFromCampaign(nested, target);
  }

  return target;
}

function collectBusinessCenterIdsFromDetail(detail, target = new Set()) {
  if (!detail || typeof detail !== 'object') return target;
  collectBusinessCenterIdsFromCampaign(detail.campaign, target);
  const bcObject = detail.business_center || detail.businessCenter;
  if (bcObject && typeof bcObject === 'object') {
    addId(target, bcObject.bc_id);
    addId(target, bcObject.id);
    addId(target, bcObject.business_center_id);
    addId(target, bcObject.businessCenterId);
  }
  return target;
}

function collectAdvertiserIdsFromCampaign(campaign, target = new Set()) {
  if (!campaign || typeof campaign !== 'object') return target;
  addId(target, campaign.advertiser_id);
  addId(target, campaign.advertiserId);

  const advertiserObject = campaign.advertiser || campaign.advertiser_info || campaign.advertiserInfo;
  if (advertiserObject && typeof advertiserObject === 'object') {
    addId(target, advertiserObject.advertiser_id);
    addId(target, advertiserObject.advertiserId);
    addId(target, advertiserObject.id);
  }

  const nested = campaign.campaign;
  if (nested && nested !== campaign) {
    collectAdvertiserIdsFromCampaign(nested, target);
  }

  return target;
}

function collectAdvertiserIdsFromDetail(detail, target = new Set()) {
  if (!detail || typeof detail !== 'object') return target;
  collectAdvertiserIdsFromCampaign(detail.campaign, target);
  const advertiserObject = detail.advertiser || detail.advertiser_info || detail.advertiserInfo;
  if (advertiserObject && typeof advertiserObject === 'object') {
    addId(target, advertiserObject.advertiser_id);
    addId(target, advertiserObject.advertiserId);
    addId(target, advertiserObject.id);
  }
  return target;
}

function collectStoreIdsFromCampaign(campaign, target = new Set()) {
  if (!campaign || typeof campaign !== 'object') return target;
  addId(target, campaign.store_id);
  addId(target, campaign.storeId);

  const storeObject = campaign.store || campaign.store_info || campaign.storeInfo;
  if (storeObject && typeof storeObject === 'object') {
    addId(target, storeObject.store_id);
    addId(target, storeObject.storeId);
    addId(target, storeObject.id);
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
        addId(target, item.store_id);
        addId(target, item.storeId);
        addId(target, item.id);
      } else {
        addId(target, item);
      }
    });
  });

  const nested = campaign.campaign;
  if (nested && nested !== campaign) {
    collectStoreIdsFromCampaign(nested, target);
  }

  return target;
}

function collectStoreIdsFromDetail(detail, target = new Set()) {
  if (!detail || typeof detail !== 'object') return target;
  collectStoreIdsFromCampaign(detail.campaign, target);
  const sessions = detail.sessions || detail.session_list || [];
  sessions.forEach((session) => {
    if (!session || typeof session !== 'object') return;
    addId(target, session.store_id);
    addId(target, session.storeId);
    const storeObject = session.store || session.store_info || session.storeInfo;
    if (storeObject && typeof storeObject === 'object') {
      addId(target, storeObject.store_id);
      addId(target, storeObject.storeId);
      addId(target, storeObject.id);
    }
    const products = session.product_list || session.products || [];
    products.forEach((product) => {
      if (!product || typeof product !== 'object') return;
      addId(target, product.store_id);
      addId(target, product.storeId);
    });
  });
  return target;
}

function matchesBusinessCenter(campaign, detail, detailLoading, selectedBusinessCenterId) {
  if (!selectedBusinessCenterId) return true;
  const target = normalizeIdValue(selectedBusinessCenterId);
  if (!target) return true;
  const ids = collectBusinessCenterIdsFromCampaign(campaign);
  if (ids.has(target)) return true;
  const detailIds = collectBusinessCenterIdsFromDetail(detail);
  if (detailIds.has(target)) return true;
  return Boolean(detailLoading);
}

function matchesAdvertiser(campaign, detail, detailLoading, selectedAdvertiserId) {
  if (!selectedAdvertiserId) return true;
  const target = normalizeIdValue(selectedAdvertiserId);
  if (!target) return true;
  const ids = collectAdvertiserIdsFromCampaign(campaign);
  if (ids.has(target)) return true;
  const detailIds = collectAdvertiserIdsFromDetail(detail);
  if (detailIds.has(target)) return true;
  return Boolean(detailLoading);
}

function matchesStore(campaign, detail, detailLoading, selectedStoreId) {
  if (!selectedStoreId) return true;
  const target = normalizeIdValue(selectedStoreId);
  if (!target) return true;
  const ids = collectStoreIdsFromCampaign(campaign);
  if (ids.has(target)) return true;
  const detailIds = collectStoreIdsFromDetail(detail);
  if (detailIds.has(target)) return true;
  return Boolean(detailLoading);
}

function matchesCampaignScope(card, filters) {
  if (!card || !card.campaign) return false;
  const { campaign, detail, detailLoading } = card;
  const { businessCenterId, advertiserId, storeId } = filters;
  if (!matchesBusinessCenter(campaign, detail, detailLoading, businessCenterId)) {
    return false;
  }
  if (!matchesAdvertiser(campaign, detail, detailLoading, advertiserId)) {
    return false;
  }
  if (!matchesStore(campaign, detail, detailLoading, storeId)) {
    return false;
  }
  return true;
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

  const reportPayload = metricsQuery.data?.report ?? metricsQuery.data?.data ?? metricsQuery.data ?? null;
  const metricsSummary = reportPayload ? summariseMetrics(reportPayload) : null;
  const productCount = Array.isArray(detail?.sessions)
    ? detail.sessions.reduce((acc, session) => {
        const products = Array.isArray(session?.product_list) ? session.product_list.length : 0;
        return acc + products;
      }, 0)
    : null;
  const statusLabel = formatCampaignStatus(campaign?.operation_status);
  const name = campaign?.campaign_name || campaign?.name || `Campaign ${campaignId}`;

  return (
    <article className="gmvmax-campaign-card">
      <header className="gmvmax-campaign-card__header">
        <div>
          <h3>{name}</h3>
          <p className="gmvmax-campaign-card__status">{statusLabel}</p>
        </div>
      </header>
      <div className="gmvmax-campaign-card__body">
        {detailLoading ? <Loading text="Loading campaign details…" /> : null}
        <ErrorBlock error={detailError} onRetry={onRetryDetail} />
        <dl className="gmvmax-campaign-card__stats">
          <div>
            <dt>Products</dt>
            <dd>{productCount ?? '—'}</dd>
          </div>
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
  const [authId, setAuthId] = useState('');
  const [businessCenterId, setBusinessCenterId] = useState('');
  const [advertiserId, setAdvertiserId] = useState('');
  const [storeId, setStoreId] = useState('');
  const [selectedProductIds, setSelectedProductIds] = useState([]);
  const [isCreateModalOpen, setCreateModalOpen] = useState(false);
  const [editingCampaignId, setEditingCampaignId] = useState('');
  const [syncError, setSyncError] = useState(null);
  const savedScopeRef = useRef(null);
  const restorationRef = useRef({
    account: false,
    businessCenter: false,
    advertiser: false,
    store: false,
  });
  const [readyToSave, setReadyToSave] = useState(false);

  const markRestored = useCallback(
    (key) => {
      if (restorationRef.current[key]) return;
      restorationRef.current[key] = true;
      if (
        restorationRef.current.account &&
        restorationRef.current.businessCenter &&
        restorationRef.current.advertiser &&
        restorationRef.current.store
      ) {
        setReadyToSave(true);
      }
    },
    [setReadyToSave],
  );

  useEffect(() => {
    if (!workspaceId) {
      savedScopeRef.current = null;
      restorationRef.current = {
        account: false,
        businessCenter: false,
        advertiser: false,
        store: false,
      };
      setReadyToSave(false);
      return;
    }
    const scope = loadScope(workspaceId, provider);
    savedScopeRef.current = scope;
    restorationRef.current = {
      account: false,
      businessCenter: false,
      advertiser: false,
      store: false,
    };
    setReadyToSave(false);
  }, [provider, workspaceId]);

  useEffect(() => {
    setAuthId('');
    setBusinessCenterId('');
    setAdvertiserId('');
    setStoreId('');
    setSelectedProductIds([]);
  }, [workspaceId]);

  const accountsQuery = useAccountsQuery(
    workspaceId,
    provider,
    {},
    {
      enabled: Boolean(workspaceId),
    },
  );

  const businessCentersQuery = useBusinessCentersQuery(
    workspaceId,
    provider,
    authId,
    {},
    {
      enabled: Boolean(workspaceId && authId),
    },
  );

  const advertiserParams = useMemo(() => {
    const params = {};
    if (businessCenterId) params.owner_bc_id = businessCenterId;
    return params;
  }, [businessCenterId]);

  const advertisersQuery = useAdvertisersQuery(
    workspaceId,
    provider,
    authId,
    advertiserParams,
    {
      enabled: Boolean(workspaceId && authId),
    },
  );

  const storesQuery = useStoresQuery(
    workspaceId,
    provider,
    authId,
    {
      advertiserId: advertiserId || undefined,
      owner_bc_id: businessCenterId || undefined,
    },
    {
      enabled: Boolean(workspaceId && authId && advertiserId),
    },
  );

  const productParams = useMemo(
    () => ({ store_id: storeId || undefined, page_size: clampPageSize(50) }),
    [storeId],
  );

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    productParams,
    {
      enabled: Boolean(workspaceId && authId && storeId),
    },
  );

  const campaignParams = useMemo(() => {
    const params = { page_size: clampPageSize(50) };
    if (storeId) params.store_ids = [String(storeId)];
    if (advertiserId) params.advertiser_id = advertiserId;
    return params;
  }, [storeId, advertiserId]);

  const campaignsQuery = useGmvMaxCampaignsQuery(
    workspaceId,
    provider,
    authId,
    campaignParams,
    {
      enabled: Boolean(workspaceId && authId),
    },
  );

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

  const businessCenters = useMemo(() => {
    const data = businessCentersQuery.data;
    const items = data?.items || data?.list || data || [];
    return Array.isArray(items) ? items : [];
  }, [businessCentersQuery.data]);

  const advertisers = useMemo(() => {
    const data = advertisersQuery.data;
    const items = data?.items || data?.list || data || [];
    return Array.isArray(items) ? items : [];
  }, [advertisersQuery.data]);

  const stores = useMemo(() => {
    const data = storesQuery.data;
    const items = data?.items || data?.list || data || [];
    return Array.isArray(items) ? items : [];
  }, [storesQuery.data]);

  useEffect(() => {
    if (restorationRef.current.account) return;
    const saved = savedScopeRef.current;
    if (
      !workspaceId ||
      !saved ||
      saved.workspaceId !== String(workspaceId) ||
      (saved.provider && saved.provider !== provider)
    ) {
      markRestored('account');
      return;
    }
    if (accountsQuery.isLoading || accountsQuery.isFetching) return;
    const savedAccountId = saved.accountAuthId ? String(saved.accountAuthId) : '';
    if (savedAccountId && accountOptions.some((option) => option.value === savedAccountId)) {
      if (authId !== savedAccountId) {
        setAuthId(savedAccountId);
      }
    }
    markRestored('account');
  }, [
    accountOptions,
    accountsQuery.isFetching,
    accountsQuery.isLoading,
    authId,
    markRestored,
    provider,
    workspaceId,
  ]);

  useEffect(() => {
    if (restorationRef.current.businessCenter) return;
    const saved = savedScopeRef.current;
    if (
      !workspaceId ||
      !saved ||
      saved.workspaceId !== String(workspaceId) ||
      (saved.provider && saved.provider !== provider)
    ) {
      markRestored('businessCenter');
      return;
    }
    const savedAccountId = saved.accountAuthId ? String(saved.accountAuthId) : '';
    if (savedAccountId && authId !== savedAccountId) {
      if (
        !accountsQuery.isLoading &&
        !accountsQuery.isFetching &&
        !accountOptions.some((option) => option.value === savedAccountId)
      ) {
        markRestored('businessCenter');
      }
      return;
    }
    if (!authId) {
      markRestored('businessCenter');
      return;
    }
    if (businessCentersQuery.isLoading || businessCentersQuery.isFetching) return;
    if (saved.businessCenterId !== undefined) {
      if (saved.businessCenterId === null) {
        if (businessCenterId !== '') {
          setBusinessCenterId('');
          return;
        }
      } else {
        const normalized = String(saved.businessCenterId);
        const hasSaved = businessCenters.some((bc) => String(bc.bc_id ?? bc.id) === normalized);
        if (hasSaved) {
          if (businessCenterId !== normalized) {
            setBusinessCenterId(normalized);
            return;
          }
        }
      }
    }
    markRestored('businessCenter');
  }, [
    accountOptions,
    accountsQuery.isFetching,
    accountsQuery.isLoading,
    authId,
    businessCenterId,
    businessCenters,
    businessCentersQuery.isFetching,
    businessCentersQuery.isLoading,
    markRestored,
    provider,
    workspaceId,
  ]);

  useEffect(() => {
    if (restorationRef.current.advertiser) return;
    const saved = savedScopeRef.current;
    if (
      !workspaceId ||
      !saved ||
      saved.workspaceId !== String(workspaceId) ||
      (saved.provider && saved.provider !== provider)
    ) {
      markRestored('advertiser');
      return;
    }
    const savedAccountId = saved.accountAuthId ? String(saved.accountAuthId) : '';
    if (savedAccountId && authId !== savedAccountId) {
      if (
        !accountsQuery.isLoading &&
        !accountsQuery.isFetching &&
        !accountOptions.some((option) => option.value === savedAccountId)
      ) {
        markRestored('advertiser');
      }
      return;
    }
    if (!authId) {
      markRestored('advertiser');
      return;
    }
    const savedBusinessCenterId = saved.businessCenterId;
    if (savedBusinessCenterId !== undefined) {
      if (savedBusinessCenterId === null) {
        if (businessCenterId !== '') {
          if (businessCentersQuery.isLoading || businessCentersQuery.isFetching) {
            return;
          }
          setBusinessCenterId('');
          return;
        }
      } else {
        const normalized = String(savedBusinessCenterId);
        if (businessCenterId !== normalized) {
          if (businessCentersQuery.isLoading || businessCentersQuery.isFetching) {
            return;
          }
          const hasSavedBc = businessCenters.some((bc) => String(bc.bc_id ?? bc.id) === normalized);
          if (hasSavedBc) {
            return;
          }
        }
      }
    }
    if (advertisersQuery.isLoading || advertisersQuery.isFetching) return;
    if (saved.advertiserId !== undefined) {
      if (saved.advertiserId === null) {
        if (advertiserId !== '') {
          setAdvertiserId('');
          return;
        }
      } else {
        const normalized = String(saved.advertiserId);
        const hasSavedAdvertiser = advertisers.some(
          (adv) => String(adv.advertiser_id ?? adv.id) === normalized,
        );
        if (hasSavedAdvertiser) {
          if (advertiserId !== normalized) {
            setAdvertiserId(normalized);
            return;
          }
        }
      }
    }
    markRestored('advertiser');
  }, [
    accountOptions,
    accountsQuery.isFetching,
    accountsQuery.isLoading,
    advertisers,
    advertisersQuery.isFetching,
    advertisersQuery.isLoading,
    advertiserId,
    authId,
    businessCenterId,
    businessCenters,
    businessCentersQuery.isFetching,
    businessCentersQuery.isLoading,
    markRestored,
    provider,
    workspaceId,
  ]);

  useEffect(() => {
    if (restorationRef.current.store) return;
    const saved = savedScopeRef.current;
    if (
      !workspaceId ||
      !saved ||
      saved.workspaceId !== String(workspaceId) ||
      (saved.provider && saved.provider !== provider)
    ) {
      markRestored('store');
      return;
    }
    const savedAccountId = saved.accountAuthId ? String(saved.accountAuthId) : '';
    if (savedAccountId && authId !== savedAccountId) {
      if (
        !accountsQuery.isLoading &&
        !accountsQuery.isFetching &&
        !accountOptions.some((option) => option.value === savedAccountId)
      ) {
        markRestored('store');
      }
      return;
    }
    if (!authId) {
      markRestored('store');
      return;
    }
    const savedBusinessCenterId = saved.businessCenterId;
    if (savedBusinessCenterId !== undefined) {
      if (savedBusinessCenterId === null) {
        if (businessCenterId !== '') {
          if (businessCentersQuery.isLoading || businessCentersQuery.isFetching) {
            return;
          }
          setBusinessCenterId('');
          return;
        }
      } else {
        const normalizedBc = String(savedBusinessCenterId);
        if (businessCenterId !== normalizedBc) {
          if (businessCentersQuery.isLoading || businessCentersQuery.isFetching) {
            return;
          }
          const hasSavedBc = businessCenters.some((bc) => String(bc.bc_id ?? bc.id) === normalizedBc);
          if (hasSavedBc) {
            return;
          }
        }
      }
    }
    const savedAdvertiserId = saved.advertiserId;
    if (savedAdvertiserId !== undefined) {
      if (savedAdvertiserId === null) {
        if (advertiserId !== '') {
          if (advertisersQuery.isLoading || advertisersQuery.isFetching) {
            return;
          }
          setAdvertiserId('');
          return;
        }
      } else {
        const normalizedAdvertiser = String(savedAdvertiserId);
        if (advertiserId !== normalizedAdvertiser) {
          if (advertisersQuery.isLoading || advertisersQuery.isFetching) {
            return;
          }
          const hasSavedAdvertiser = advertisers.some(
            (adv) => String(adv.advertiser_id ?? adv.id) === normalizedAdvertiser,
          );
          if (hasSavedAdvertiser) {
            return;
          }
        }
      }
    }
    if (storesQuery.isLoading || storesQuery.isFetching) return;
    if (saved.storeId !== undefined) {
      if (saved.storeId === null) {
        if (storeId !== '') {
          setStoreId('');
          return;
        }
      } else {
        const normalized = String(saved.storeId);
        const hasSavedStore = stores.some((store) => String(store.store_id ?? store.id) === normalized);
        if (hasSavedStore) {
          if (storeId !== normalized) {
            setStoreId(normalized);
            return;
          }
        }
      }
    }
    markRestored('store');
  }, [
    accountOptions,
    accountsQuery.isFetching,
    accountsQuery.isLoading,
    advertiserId,
    advertisers,
    advertisersQuery.isFetching,
    advertisersQuery.isLoading,
    authId,
    businessCenterId,
    businessCenters,
    businessCentersQuery.isFetching,
    businessCentersQuery.isLoading,
    markRestored,
    provider,
    storeId,
    stores,
    storesQuery.isFetching,
    storesQuery.isLoading,
    workspaceId,
  ]);

  useEffect(() => {
    if (!authId && accountOptions.length === 1) {
      setAuthId(accountOptions[0].value);
    }
  }, [accountOptions, authId]);

  useEffect(() => {
    if (!authId) return;
    if (accountOptions.some((option) => option.value === authId)) return;
    setAuthId('');
  }, [accountOptions, authId]);

  useEffect(() => {
    if (authId) return;
    setBusinessCenterId('');
    setAdvertiserId('');
    setStoreId('');
    setSelectedProductIds([]);
  }, [authId]);

  useEffect(() => {
    if (businessCenterId) return;
    setAdvertiserId('');
    setStoreId('');
    setSelectedProductIds([]);
  }, [businessCenterId]);

  useEffect(() => {
    if (advertiserId) return;
    setStoreId('');
    setSelectedProductIds([]);
  }, [advertiserId]);

  useEffect(() => {
    if (storeId) return;
    setSelectedProductIds([]);
  }, [storeId]);

  useEffect(() => {
    if (!readyToSave) return;
    if (!workspaceId) return;
    saveScope(workspaceId, provider, {
      accountAuthId: authId || undefined,
      businessCenterId: businessCenterId === '' ? null : businessCenterId,
      advertiserId: advertiserId === '' ? null : advertiserId,
      storeId: storeId === '' ? null : storeId,
    });
  }, [authId, advertiserId, businessCenterId, provider, readyToSave, storeId, workspaceId]);

  const storeNameById = useMemo(() => {
    const map = new Map();
    stores.forEach((store) => {
      const id = store.store_id ?? store.id;
      if (id !== undefined && id !== null) {
        map.set(String(id), store.name || store.store_name || String(id));
      }
    });
    return map;
  }, [stores]);

  const products = useMemo(() => {
    const data = productsQuery.data;
    const items = data?.items || data?.list || data || [];
    return Array.isArray(items) ? items : [];
  }, [productsQuery.data]);

  const campaigns = useMemo(() => {
    if (!authId) return [];
    const data = campaignsQuery.data;
    const items = data?.items || data?.list || data || [];
    return Array.isArray(items) ? items : [];
  }, [authId, campaignsQuery.data]);

  const campaignDetailQueries = useQueries({
    queries: campaigns.map((campaign) => {
      const campaignId = campaign?.campaign_id || campaign?.id;
      return {
        queryKey: ['gmvMax', 'campaign-detail', workspaceId, provider, authId, campaignId],
        queryFn: () => getGmvMaxCampaign(workspaceId, provider, authId, campaignId),
        enabled: Boolean(workspaceId && authId && campaignId),
        staleTime: 60 * 1000,
      };
    }),
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
    campaignDetailQueries.forEach((result) => {
      const detail = result?.data;
      if (!detail) return;
      const sessions = detail.sessions || detail.session_list || [];
      sessions.forEach((session) => {
        (session?.product_list || session?.products || []).forEach((product) => {
          const id = getProductIdentifier(product);
          if (id) {
            ids.add(id);
          }
        });
      });
    });
    return ids;
  }, [campaignDetailQueries]);

  const unassignedProducts = useMemo(() => {
    if (products.length === 0) return [];
    return products.filter((product) => {
      const id = getProductIdentifier(product);
      if (!id) return false;
      if (!isProductAvailable(product)) return false;
      return !assignedProductIds.has(id);
    });
  }, [assignedProductIds, products]);

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
        };
      }),
    [campaignDetailsById, campaigns],
  );

  const filteredCampaignCards = useMemo(() => {
    if (!authId) return [];
    return campaignCards.filter((card) =>
      matchesCampaignScope(card, {
        businessCenterId,
        advertiserId,
        storeId,
      }),
    );
  }, [advertiserId, authId, businessCenterId, campaignCards, storeId]);

  const syncMutation = useSyncGmvMaxCampaignsMutation(workspaceId, provider, authId, {
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'campaigns', workspaceId, provider, authId] });
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'products', workspaceId, provider, authId] });
    },
  });

  const canSync = Boolean(authId && storeId);
  const canCreateSeries = Boolean(authId && storeId);

  const handleSync = useCallback(async () => {
    if (!canSync) return;
    setSyncError(null);
    const range = getRecentDateRange(7);
      const payload = {
        advertiser_id: advertiserId ? String(advertiserId) : undefined,
        campaign_filter: storeId ? { store_ids: [String(storeId)] } : undefined,
        campaign_options: { page_size: clampPageSize(50) },
        report: {
          store_ids: storeId ? [String(storeId)] : undefined,
          start_date: range.start,
          end_date: range.end,
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
    canSync,
    campaignsQuery,
    productsQuery,
    storeId,
    syncMutation,
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

  const campaignsLoading = Boolean(authId) && (campaignsQuery.isLoading || campaignsQuery.isFetching);
  const productsLoading = productsQuery.isLoading || productsQuery.isFetching;

  const selectedAccountLabel = accountOptions.find((item) => item.value === authId)?.label || '';

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
              <select
                value={authId}
                onChange={(event) => {
                  setAuthId(event.target.value);
                  setBusinessCenterId('');
                  setAdvertiserId('');
                  setStoreId('');
                }}
              >
                <option value="">Select account</option>
                {accountOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                    {option.status === 'invalid' ? ' (invalid)' : ''}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField label="Business center (optional)">
              <select
                value={businessCenterId}
                onChange={(event) => {
                  setBusinessCenterId(event.target.value);
                  setAdvertiserId('');
                  setStoreId('');
                }}
                disabled={!authId}
              >
                <option value="">All business centers</option>
                {businessCenters.map((bc) => (
                  <option key={bc.bc_id || bc.id} value={bc.bc_id || bc.id}>
                    {bc.name || bc.bc_id || bc.id}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField label="Advertiser">
              <select
                value={advertiserId}
                onChange={(event) => {
                  setAdvertiserId(event.target.value);
                  setStoreId('');
                }}
                disabled={!authId}
              >
                <option value="">Select advertiser</option>
                {advertisers.map((adv) => (
                  <option key={adv.advertiser_id || adv.id} value={adv.advertiser_id || adv.id}>
                    {adv.display_name || adv.name || adv.advertiser_id || adv.id}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField label="Store">
              <select
                value={storeId}
                onChange={(event) => setStoreId(event.target.value)}
                disabled={!advertiserId}
              >
                <option value="">Select store</option>
                {stores.map((store) => (
                  <option key={store.store_id || store.id} value={store.store_id || store.id}>
                    {store.name || store.store_name || store.store_id || store.id}
                  </option>
                ))}
              </select>
            </FormField>
          </div>
          <div className="gmvmax-card__footer">
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
          {authId && !storeId ? (
            <p className="gmvmax-placeholder">Choose an advertiser and store to load products.</p>
          ) : null}
          {authId && storeId ? (
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
          <ErrorBlock error={productsQuery.error} onRetry={productsQuery.refetch} />
        </div>
      </section>

      <section className="gmvmax-card">
        <header className="gmvmax-card__header">
          <h2>GMV Max series</h2>
        </header>
        <div className="gmvmax-card__body">
          <SeriesErrorNotice
            error={authId ? campaignsQuery.error : null}
            onRetry={campaignsQuery.refetch}
          />
          {campaignsLoading ? <Loading text="Loading campaigns…" /> : null}
          {!campaignsLoading && !campaignsQuery.error && (!authId || filteredCampaignCards.length === 0) ? (
            <p className="gmvmax-placeholder">
              No GMV Max series found for the selected scope.
              {!authId ? ' Select an account to get started.' : ''}
            </p>
          ) : null}
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

