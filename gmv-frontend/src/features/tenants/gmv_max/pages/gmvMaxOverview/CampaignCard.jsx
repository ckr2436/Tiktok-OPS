import { useCallback, useMemo } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import Loading from '@/components/ui/Loading.jsx';

import {
  collectProductIdsFromCampaign,
  collectProductIdsFromDetail,
  extractProductsFromDetail,
  formatCampaignStatus,
  formatError,
  formatISODate,
  formatMoney,
  formatRoi,
  getCampaignStatusMeta,
  isCampaignEnabledStatus,
} from './helpers.js';
import { ErrorBlock } from './ErrorHandling.jsx';
import { useApplyGmvMaxActionMutation } from '../../hooks/gmvMaxQueries.js';
import { GmvMaxTexts } from '../../locale.js';

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
  storeName,
  onEdit,
  onManage,
  onDashboard,
  products,
  metricsSummary,
  metricsLoading = false,
  metricsError = null,
  isDeleted = false,
}) {
  const campaignId = campaign?.campaign_id || campaign?.id;
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

  const productCount = useMemo(() => {
    if (detail) {
      return collectProductIdsFromDetail(detail).size;
    }
    return collectProductIdsFromCampaign(campaign).size;
  }, [detail, campaign]);
  const statusMeta = getCampaignStatusMeta(
    campaign?.operation_status || campaign?.status || detail?.campaign?.operation_status || detail?.campaign?.status,
    { isDeleted, deletedLabel: GmvMaxTexts.statusDeleted || '已删除' },
  );
  const statusLabel = statusMeta.label || formatCampaignStatus(campaign?.operation_status);
  const name = campaign?.campaign_name || campaign?.name || `系列 ${campaignId}`;
  const storeLabel =
    storeName ||
    campaign?.store_name ||
    campaign?.storeName ||
    detail?.campaign?.store_name ||
    detail?.campaign?.storeName ||
    storeId ||
    '';
  const startTime =
    detail?.campaign?.start_time || detail?.campaign?.startTime || campaign?.start_time || campaign?.startTime;
  const endTime = detail?.campaign?.end_time || detail?.campaign?.endTime || campaign?.end_time || campaign?.endTime;
  const timelineLabel = startTime || endTime
    ? `${startTime ? formatISODate(startTime) : '—'} 至 ${endTime ? formatISODate(endTime) : '—'}`
    : null;
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
    if (isDeleted) return;
    actionMutation.mutate({ type: 'resume' });
  }, [actionMutation, campaignId, isDeleted]);

  const handleDisable = useCallback(() => {
    if (!campaignId) return;
    if (isDeleted) return;
    actionMutation.mutate({ type: 'pause' });
  }, [actionMutation, campaignId, isDeleted]);

  const handleDelete = useCallback(() => {
    if (!campaignId) return;
    if (isDeleted) return;
    const confirmed = window.confirm(GmvMaxTexts.deleteSeries);
    if (!confirmed) return;
    actionMutation.mutate({ type: 'delete' });
  }, [actionMutation, campaignId, isDeleted]);

  return (
    <article className="gmvmax-campaign-card">
      <header className="gmvmax-campaign-card__header">
        <div className="gmvmax-campaign-card__title">
          <h3 title={name}>{name}</h3>
          <div className="gmvmax-campaign-card__meta">
            <p className={`gmvmax-status-badge gmvmax-status-badge--${statusMeta.tone || 'muted'}`}>
              {statusLabel}
            </p>
            {storeLabel ? <span className="gmvmax-campaign-card__store">{storeLabel}</span> : null}
            {timelineLabel ? (
              <span className="gmvmax-campaign-card__timeline" title={GmvMaxTexts.viewTimeline}>
                {timelineLabel}
              </span>
            ) : null}
          </div>
        </div>
        {!isDeleted ? (
          <div className="gmvmax-campaign-card__toggles" aria-label="系列操作">
            <button
              type="button"
              className={`gmvmax-toggle-button ${isEnabled ? 'gmvmax-toggle-button--active' : ''}`}
              aria-label={GmvMaxTexts.toggleEnableTooltip}
              aria-pressed={isEnabled}
              onClick={handleEnable}
              disabled={isEnabled || actionMutation.isPending}
              title={GmvMaxTexts.toggleEnableTooltip}
            >
              <span aria-hidden="true">▶</span>
            </button>
            <button
              type="button"
              className={`gmvmax-toggle-button ${!isEnabled ? 'gmvmax-toggle-button--active' : ''}`}
              aria-label={GmvMaxTexts.togglePauseTooltip}
              aria-pressed={!isEnabled}
              onClick={handleDisable}
              disabled={!isEnabled || actionMutation.isPending}
              title={GmvMaxTexts.togglePauseTooltip}
            >
              <span aria-hidden="true">⏸</span>
            </button>
          </div>
        ) : null}
      </header>
      {actionError ? <p className="gmvmax-campaign-card__action-error">{actionError}</p> : null}
      <div className="gmvmax-campaign-card__body">
        {detailLoading ? <Loading text="加载系列详情…" /> : null}
        <ErrorBlock error={detailError} onRetry={onRetryDetail} />
        <div className="gmvmax-campaign-card__products">
          <div className="gmvmax-campaign-card__products-count">
            <span>{GmvMaxTexts.products}</span>
            <strong>{productCount ?? '—'}</strong>
          </div>
          {detailLoading ? (
            <span className="gmvmax-campaign-card__products-placeholder">商品加载中…</span>
          ) : displayedProducts.length === 0 ? (
            <span className="gmvmax-campaign-card__products-placeholder">暂无商品预览。</span>
          ) : (
            <div className="gmvmax-product-thumbnails" aria-label="商品预览">
              {displayedProducts.map((product, index) => {
                const key = product.id || product.name || `product-${index}`;
                return (
                  <div key={key} className="gmvmax-product-thumbnail" title={product.name}>
                    {product.image ? (
                      <img src={product.image} alt={product.name || '商品'} />
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
            <dt>{GmvMaxTexts.totalSpend}（{GmvMaxTexts.metricsWindowLabel}）</dt>
            <dd>
              {metricsLoading ? '加载中…' : metricsSummary ? formatMoney(metricsSummary.spend) : '—'}
            </dd>
          </div>
          <div>
            <dt>{GmvMaxTexts.totalGmv}（{GmvMaxTexts.metricsWindowLabel}）</dt>
            <dd>
              {metricsLoading ? '加载中…' : metricsSummary ? formatMoney(metricsSummary.gmv) : '—'}
            </dd>
          </div>
          <div>
            <dt>{GmvMaxTexts.averageRoas}（{GmvMaxTexts.metricsWindowLabel}）</dt>
            <dd>
              {metricsLoading
                ? '加载中…'
                : metricsSummary && metricsSummary.roas !== null
                ? formatRoi(metricsSummary.roas)
                : '—'}
            </dd>
          </div>
        </dl>
        <ErrorBlock error={metricsError} />
      </div>
      <footer className="gmvmax-campaign-card__footer">
        {isDeleted ? (
          <button
            type="button"
            className="gmvmax-button gmvmax-button--secondary"
            onClick={() => onDashboard?.(campaignId)}
          >
            {GmvMaxTexts.viewData}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="gmvmax-button gmvmax-button--secondary"
              onClick={() => onEdit?.(campaignId)}
              disabled={!detail || detailLoading}
            >
              {GmvMaxTexts.editSeries}
            </button>
            <button
              type="button"
              className="gmvmax-button gmvmax-button--secondary"
              onClick={() => onManage?.(campaignId)}
            >
              {GmvMaxTexts.manageProducts}
            </button>
            <button
              type="button"
              className="gmvmax-button gmvmax-button--secondary"
              onClick={() => onDashboard?.(campaignId)}
            >
              {GmvMaxTexts.dashboard}
            </button>
            <button
              type="button"
              className="gmvmax-button gmvmax-button--danger"
              onClick={handleDelete}
              disabled={actionMutation.isPending}
            >
              {GmvMaxTexts.deleteSeries}
            </button>
          </>
        )}
      </footer>
    </article>
  );
}
