import { useCallback, useMemo } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import Loading from '@/components/ui/Loading.jsx';

import {
  collectProductIdsFromCampaign,
  collectProductIdsFromDetail,
  extractProductsFromDetail,
  formatCampaignStatus,
  formatError,
  formatMoney,
  formatRoi,
  getRecentDateRange,
  isCampaignEnabledStatus,
  summariseMetrics,
} from './helpers.js';
import { ErrorBlock } from './ErrorHandling.jsx';
import {
  useApplyGmvMaxActionMutation,
  useGmvMaxMetricsQuery,
} from '../../hooks/gmvMaxQueries.js';

export default function CampaignCard({
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
  products,
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
  const productCount = useMemo(() => {
    if (detail) {
      return collectProductIdsFromDetail(detail).size;
    }
    return collectProductIdsFromCampaign(campaign).size;
  }, [detail, campaign]);
  const statusLabel = formatCampaignStatus(campaign?.operation_status);
  const name = campaign?.campaign_name || campaign?.name || `Campaign ${campaignId}`;
  const previewProducts = useMemo(() => {
    const extracted = extractProductsFromDetail(detail);
    if (Array.isArray(extracted) && extracted.length > 0) {
      return extracted;
    }
    const ids = new Set();
    collectProductIdsFromCampaign(campaign, ids);
    const result = [];
    const productMap = new Map();
    (products || []).forEach((item) => {
      const pid = item?.spu_id || item?.spuId || item?.product_id || item?.productId || item?.id;
      if (pid) {
        productMap.set(String(pid), item);
      }
    });
    ids.forEach((pid) => {
      const item = productMap.get(pid);
      if (item) {
        const nameCandidate =
          item.title ||
          item.name ||
          item.product_name ||
          item.productName ||
          item.item_name ||
          item.itemName ||
          pid ||
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
        result.push({ id: pid, name: nameCandidate, image });
      }
    });
    return result;
  }, [detail, campaign, products]);
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
          className="gmvmax-button gmvmax-button--secondary"
          onClick={() => onManage?.(campaignId)}
        >
          Manage products
        </button>
        <button
          type="button"
          className="gmvmax-button gmvmax-button--secondary"
          onClick={() => onDashboard?.(campaignId)}
        >
          Dashboard
        </button>
        <button
          type="button"
          className="gmvmax-button gmvmax-button--danger"
          onClick={handleDelete}
          disabled={actionMutation.isPending}
        >
          Delete
        </button>
      </footer>
    </article>
  );
}
