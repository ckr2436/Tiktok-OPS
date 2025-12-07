import { useCallback, useEffect, useMemo, useState } from 'react';

import Modal from '@/components/ui/Modal.jsx';
import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';

import ProductSelectionPanel from './ProductSelectionPanel.jsx';
import { ErrorBlock } from './ErrorHandling.jsx';
import {
  formatError,
  getAvailableProductIds,
  getProductIdentifier,
  parseOptionalFloat,
  setsEqual,
  ensureArray,
  getStoreId,
  getStoreLabel,
} from './helpers.js';
import {
  useUpdateGmvMaxCampaignMutation,
  useUpdateGmvMaxStrategyMutation,
  useGmvMaxIdentitiesQuery,
  useProductsQuery,
} from '../../hooks/gmvMaxQueries.js';
import { GmvMaxTexts } from '../../locale.js';

export default function EditSeriesModal({
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
  const [selectedStoreId, setSelectedStoreId] = useState('');
  const [selectedIdentities, setSelectedIdentities] = useState(new Set());
  const [startTime, setStartTime] = useState('');
  const [endTime, setEndTime] = useState('');
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

  const campaignStoreBcId = useMemo(() => {
    const store = detail?.campaign?.store || {};
    return (
      detail?.campaign?.store_authorized_bc_id ||
      detail?.campaign?.authorized_bc_id ||
      detail?.campaign?.bc_id ||
      store.store_authorized_bc_id ||
      store.bc_id ||
      undefined
    );
  }, [detail]);

  const initialIdentities = useMemo(() => {
    const list = ensureArray(
      detail?.campaign?.identities || detail?.campaign?.identity_list || detail?.campaign?.identityList,
    )
      .map((item) => (typeof item === 'object' ? item.identity_id || item.id : item))
      .filter(Boolean)
      .map(String);
    return new Set(list);
  }, [detail]);

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    {
      store_id: selectedStoreId || storeId || undefined,
      advertiser_id: campaign?.advertiser_id || campaign?.advertiserId || undefined,
      owner_bc_id: campaignStoreBcId,
      gmv_max_ads_status: 'UNOCCUPIED',
      page_size: 50,
    },
    {
      enabled: Boolean(open && workspaceId && provider && authId && (selectedStoreId || storeId)),
    },
  );

  const mergedProducts = useMemo(() => {
    const map = new Map();
    const queried = Array.isArray(productsQuery.data?.items)
      ? productsQuery.data.items
      : Array.isArray(productsQuery.data?.list)
        ? productsQuery.data.list
        : Array.isArray(productsQuery.data)
          ? productsQuery.data
          : [];
    (products || []).forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) {
        map.set(id, product);
      }
    });
    queried.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id && !map.has(id)) {
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
  }, [detailProducts, products, productsQuery.data]);

  const availableProductIds = useMemo(() => getAvailableProductIds(mergedProducts), [mergedProducts]);

  const identityParams = useMemo(
    () => ({
      store_id: selectedStoreId || storeId || undefined,
      advertiser_id: campaign?.advertiser_id || campaign?.advertiserId || undefined,
      store_authorized_bc_id:
        detail?.campaign?.store_authorized_bc_id || detail?.campaign?.authorized_bc_id || undefined,
    }),
    [campaign, detail, selectedStoreId, storeId],
  );

  const identitiesQuery = useGmvMaxIdentitiesQuery(workspaceId, provider, authId, identityParams, {
    enabled: Boolean(open && workspaceId && provider && authId && (selectedStoreId || storeId)),
  });

  const identityOptions = useMemo(() => {
    const payload = identitiesQuery.data || {};
    const list = ensureArray(payload.identities || payload.identity_list || payload.items);
    return list
      .filter((identity) => identity?.product_gmv_max_available !== false)
      .map((identity) => ({
        value: String(identity.identity_id || identity.id || identity.identityId || ''),
        label: identity.identity_name || identity.name || identity.identityName || '',
      }))
      .filter((option) => option.value);
  }, [identitiesQuery.data]);

  const storeOptions = useMemo(() => {
    const options = [];
    if (storeNameById && typeof storeNameById.forEach === 'function') {
      storeNameById.forEach((label, id) => {
        if (id) options.push({ value: String(id), label: label || String(id) });
      });
    }
    const campaignStoreId = detail?.campaign?.store_id || detail?.campaign?.storeId;
    if (campaignStoreId && !options.find((item) => item.value === String(campaignStoreId))) {
      options.push({ value: String(campaignStoreId), label: getStoreLabel(detail?.campaign?.store || {}) || String(campaignStoreId) });
    }
    return options;
  }, [detail, storeNameById]);

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
    setSelectedStoreId(detail.campaign?.store_id || detail.campaign?.storeId || storeId || '');
    setStartTime(detail.campaign?.start_time || detail.campaign?.startTime || '');
    setEndTime(detail.campaign?.end_time || detail.campaign?.endTime || '');
    const identityList = ensureArray(
      detail.campaign?.identities || detail.campaign?.identity_list || detail.campaign?.identityList,
    )
      .map((item) => (typeof item === 'object' ? item.identity_id || item.id : item))
      .filter(Boolean)
      .map(String);
    setSelectedIdentities(new Set(identityList));
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

  useEffect(() => {
    if (!open) return;
    setLocalSelectedIds((prev) => {
      const next = new Set();
      prev.forEach((id) => {
        if (availableProductIds.has(id)) {
          next.add(id);
        }
      });
      return next;
    });
  }, [availableProductIds, open]);

  useEffect(() => {
    if (!open) return;
    setSelectedIdentities((prev) => new Set(prev));
  }, [open, selectedStoreId]);

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

  const handleIdentityChange = useCallback((event) => {
    const values = Array.from(event.target.selectedOptions || []).map((option) => option.value);
    const limited = values.slice(0, 4).map(String);
    setSelectedIdentities(new Set(limited));
  }, []);

  const updateCampaignMutation = useUpdateGmvMaxCampaignMutation(workspaceId, provider, authId, campaignId);
  const strategyMutation = useUpdateGmvMaxStrategyMutation(workspaceId, provider, authId, campaignId);

  const productsChanged = useMemo(
    () => !setsEqual(localSelectedIds, initialProductSet),
    [localSelectedIds, initialProductSet],
  );

  const identityList = useMemo(
    () => Array.from(selectedIdentities).map(String).filter(Boolean),
    [selectedIdentities],
  );

  const identitiesChanged = useMemo(
    () => !setsEqual(new Set(identityList), initialIdentities),
    [identityList, initialIdentities],
  );

  const sessionId = detail?.sessions?.[0]?.session_id || detail?.session?.session_id || null;
  const effectiveStoreId = selectedStoreId || storeId || detail?.campaign?.store_id || null;

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
    if (startTime && startTime !== detail?.campaign?.start_time) {
      campaignPatch.start_time = startTime;
    }
    if (endTime !== undefined && endTime !== detail?.campaign?.end_time) {
      campaignPatch.end_time = endTime;
    }
    if (identitiesChanged) {
      campaignPatch.identities = identityList;
    }

    const tasks = [];
    setSubmitError(null);

    try {
      if (Object.keys(campaignPatch).length > 0) {
        tasks.push(updateCampaignMutation.mutateAsync(campaignPatch));
      }
      const needsSessionUpdate = productsChanged || identitiesChanged;
      if (needsSessionUpdate) {
        if (!sessionId) {
          throw new Error('Unable to update products: missing session information.');
        }
        const productList = Array.from(localSelectedIds).map((id) => ({ spu_id: String(id) }));
        const sessionPayload = {
          session_id: sessionId,
          store_id: effectiveStoreId ? String(effectiveStoreId) : undefined,
          product_list: productList,
        };
        if (identityList.length > 0) {
          sessionPayload.identities = identityList;
        }
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
    identityList,
    identitiesChanged,
    startTime,
    endTime,
  ]);

  const isSaving = updateCampaignMutation.isPending || strategyMutation.isPending;
  const startChanged = Boolean(startTime && startTime !== detail?.campaign?.start_time);
  const endChanged = Boolean(endTime !== detail?.campaign?.end_time);
  const canSubmit =
    Boolean(detail) &&
    (productsChanged ||
      (name.trim() && name.trim() !== detail?.campaign?.campaign_name) ||
      (budget && parseOptionalFloat(budget) !== detail?.campaign?.budget) ||
      (roasBid && parseOptionalFloat(roasBid) !== detail?.campaign?.roas_bid) ||
      identitiesChanged ||
      startChanged ||
      endChanged);

  if (!open) return null;

  return (
    <Modal open={open} title={GmvMaxTexts.editSeries} onClose={onClose}>
      {detailLoading ? <Loading text="系列加载中…" /> : null}
      <ErrorBlock error={detailError} onRetry={onRetryDetail} />
      {!detailLoading && !detailError && !detail ? <p>无法获取系列详情。</p> : null}
      {!detail || detailLoading || detailError ? null : (
        <div className="gmvmax-modal-step">
          <h3>基础配置</h3>
          <FormField label="店铺">
            <select
              value={selectedStoreId}
              onChange={(event) => setSelectedStoreId(event.target.value)}
              disabled={storeOptions.length === 0}
            >
              <option value="">选择店铺</option>
              {storeOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="系列名称">
            <input type="text" value={name} onChange={(event) => setName(event.target.value)} />
          </FormField>
          <div className="gmvmax-modal-grid">
            <FormField label="预算">
              <input
                type="number"
                min="0"
                step="0.01"
                value={budget}
                onChange={(event) => setBudget(event.target.value)}
                placeholder="留空则保持不变"
              />
            </FormField>
            <FormField label="ROAS 出价">
              <input
                type="number"
                min="0"
                step="0.01"
                value={roasBid}
                onChange={(event) => setRoasBid(event.target.value)}
                placeholder="留空则保持不变"
              />
            </FormField>
          </div>
          <div className="gmvmax-modal-grid">
            <FormField label="开始时间">
              <input
                type="datetime-local"
                value={startTime}
                onChange={(event) => setStartTime(event.target.value)}
              />
            </FormField>
            <FormField label="结束时间">
              <input
                type="datetime-local"
                value={endTime}
                onChange={(event) => setEndTime(event.target.value)}
              />
            </FormField>
          </div>
          <FormField label="身份（最多选择4个）">
            {identitiesQuery.isLoading ? <Loading text="身份加载中…" /> : null}
            <select
              multiple
              value={Array.from(selectedIdentities)}
              onChange={handleIdentityChange}
              disabled={identityOptions.length === 0}
            >
              {identityOptions.length === 0 ? <option value="">暂无可用身份</option> : null}
              {identityOptions.map((identity) => (
                <option key={identity.value} value={identity.value}>
                  {identity.label || identity.value}
                </option>
              ))}
            </select>
          </FormField>
          <dl className="gmvmax-review-list">
            <div>
              <dt>优化目标</dt>
              <dd>{detail.campaign?.optimization_goal || '—'}</dd>
            </div>
            <div>
              <dt>广告投放类型</dt>
              <dd>{detail.campaign?.shopping_ads_type || '—'}</dd>
            </div>
          </dl>
          <h3>商品</h3>
          {!sessionId ? (
            <p>缺少会话信息，无法编辑商品。</p>
          ) : (
            <ProductSelectionPanel
              products={mergedProducts}
              selectedIds={localSelectedIds}
              onToggle={toggleProduct}
              onToggleAll={toggleAll}
              onSelectAll={(ids) => setLocalSelectedIds(new Set((ids || []).map(String)))}
              onClearAll={() => setLocalSelectedIds(new Set())}
              storeNames={storeNameById}
              loading={productsLoading || productsQuery.isLoading || productsQuery.isFetching}
              emptyMessage={productsQuery.isLoading ? '商品加载中…' : '未找到商品。'}
              disabled={isSaving}
            />
          )}
          {submitError ? <div className="gmvmax-error">{submitError}</div> : null}
          {isSaving ? <Loading text="保存修改中…" /> : null}
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={onClose} disabled={isSaving}>
              {GmvMaxTexts.cancel}
            </button>
            <button type="button" onClick={handleSubmit} disabled={isSaving || !canSubmit}>
              {GmvMaxTexts.saveChanges}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
