import { useCallback, useEffect, useMemo, useState } from 'react';

import Modal from '@/components/ui/Modal.jsx';
import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';

import ProductSelectionPanel from './ProductSelectionPanel.jsx';
import {
  extractChoiceList,
  formatError,
  formatMoney,
  getAvailableProductIds,
  getProductIdentifier,
  parseOptionalFloat,
  getStoreId,
  getStoreLabel,
  ensureArray,
} from './helpers.js';
import { ErrorBlock } from './ErrorHandling.jsx';
import {
  useCreateGmvMaxCampaignMutation,
  useGmvMaxOptionsQuery,
  useProductsQuery,
  useGmvMaxIdentitiesQuery,
} from '../../hooks/gmvMaxQueries.js';
import { GmvMaxTexts } from '../../locale.js';

export default function CreateSeriesModal({
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
  const [selectedStoreId, setSelectedStoreId] = useState(storeId ? String(storeId) : '');
  const [form, setForm] = useState({
    name: '',
    shoppingAdsType: '',
    optimizationGoal: '',
    bidType: '',
    budget: '',
    roasBid: '',
  });
  const [localSelectedIds, setLocalSelectedIds] = useState(new Set());
  const [selectedIdentities, setSelectedIdentities] = useState(new Set());
  const [startTime, setStartTime] = useState('');
  const [endTime, setEndTime] = useState('');
  const [submitError, setSubmitError] = useState(null);

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    {
      store_id: selectedStoreId || undefined,
      advertiser_id: advertiserId || undefined,
      gmv_max_ads_status: 'UNOCCUPIED',
      page_size: 50,
    },
    {
      enabled: Boolean(open && workspaceId && provider && authId && selectedStoreId),
    },
  );

  const productsData = useMemo(() => {
    const payload = productsQuery.data;
    const list = payload?.items || payload?.list || payload || [];
    const normalized = Array.isArray(list) ? list : [];
    if (normalized.length > 0) return normalized;
    return products || [];
  }, [products, productsQuery.data]);

  const productsById = useMemo(() => {
    const map = new Map();
    (productsData || []).forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) {
        map.set(id, product);
      }
    });
    return map;
  }, [productsData]);

  useEffect(() => {
    if (!open) return;
    setStep(1);
    setSubmitError(null);
    setSelectedStoreId(storeId ? String(storeId) : '');
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
    setSelectedIdentities(new Set());
    const nowIso = new Date().toISOString().slice(0, 16);
    const defaultEnd = new Date();
    defaultEnd.setDate(defaultEnd.getDate() + 7);
    setStartTime(nowIso);
    setEndTime(defaultEnd.toISOString().slice(0, 16));
  }, [open, initialProductIds]);

  useEffect(() => {
    if (!open) return;
    setLocalSelectedIds(new Set());
    setSelectedIdentities(new Set());
  }, [open, selectedStoreId]);

  useEffect(() => {
    if (!open) return;
    const allowed = getAvailableProductIds(productsData);
    setLocalSelectedIds((prev) => {
      const next = new Set();
      prev.forEach((id) => {
        if (allowed.has(id)) {
          next.add(id);
        }
      });
      return next;
    });
  }, [open, productsData]);

  const optionsQuery = useGmvMaxOptionsQuery(
    workspaceId,
    provider,
    authId,
    {},
    {
      enabled: Boolean(open && workspaceId && authId),
    },
  );

  const storeOptions = useMemo(() => {
    const payload = optionsQuery.data || {};
    const rawStores = ensureArray(payload.stores || payload.store_list || payload.storeList);
    return rawStores
      .filter((store) => store?.is_gmv_max_available !== false)
      .map((store) => ({
        value: getStoreId(store),
        label: getStoreLabel(store),
        data: store,
      }))
      .filter((option) => option.value);
  }, [optionsQuery.data]);

  useEffect(() => {
    if (!open || selectedStoreId) return;
    if (storeOptions.length > 0) {
      setSelectedStoreId(storeOptions[0].value);
    }
  }, [open, selectedStoreId, storeOptions]);

  const selectedStore = useMemo(() => {
    return storeOptions.find((option) => option.value === selectedStoreId)?.data || null;
  }, [selectedStoreId, storeOptions]);

  const identityParams = useMemo(
    () => ({
      store_id: selectedStoreId || undefined,
      advertiser_id: advertiserId || undefined,
      store_authorized_bc_id:
        selectedStore?.store_authorized_bc_id || selectedStore?.authorized_bc_id || undefined,
    }),
    [advertiserId, selectedStore, selectedStoreId],
  );

  const identitiesQuery = useGmvMaxIdentitiesQuery(workspaceId, provider, authId, identityParams, {
    enabled: Boolean(open && workspaceId && provider && authId && selectedStoreId),
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

  const requiresIdentity = useMemo(() => {
    return identityOptions.length > 0 && (form.shoppingAdsType || '').toUpperCase().includes('LIVE');
  }, [form.shoppingAdsType, identityOptions.length]);

  const canProceedStep1 = Boolean(
    form.name.trim() &&
      form.optimizationGoal &&
      form.shoppingAdsType &&
      selectedStoreId &&
      (!requiresIdentity || selectedIdentities.size > 0),
  );
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

  const selectAllProducts = useCallback((ids) => {
    const normalized = (ids || []).map(String);
    setLocalSelectedIds(new Set(normalized));
  }, []);

  const clearAllProducts = useCallback(() => {
    setLocalSelectedIds(new Set());
  }, []);

  const handleIdentityChange = useCallback((event) => {
    const values = Array.from(event.target.selectedOptions || []).map((option) => option.value);
    const limited = values.slice(0, 4).map(String);
    setSelectedIdentities(new Set(limited));
  }, []);

  const handleSubmit = useCallback(async () => {
    const effectiveStoreId = selectedStoreId || storeId;
    if (!effectiveStoreId) {
      setSubmitError('请选择店铺');
      return;
    }
    if (!canProceedStep2) return;
    if (requiresIdentity && selectedIdentities.size === 0) {
      setSubmitError('请选择至少一个身份');
      return;
    }
    const trimmedName = form.name.trim();
    const payload = {
      campaign: {
        campaign_name: trimmedName,
        shopping_ads_type: form.shoppingAdsType || undefined,
        optimization_goal: form.optimizationGoal || undefined,
        bid_type: form.bidType || undefined,
        advertiser_id: advertiserId ? String(advertiserId) : undefined,
        store_id: effectiveStoreId ? String(effectiveStoreId) : undefined,
        start_time: startTime || undefined,
        end_time: endTime || undefined,
      },
      session: {
        store_id: effectiveStoreId ? String(effectiveStoreId) : undefined,
        product_list: Array.from(localSelectedIds).map((id) => ({ spu_id: String(id) })),
      },
    };

    const identityList = Array.from(selectedIdentities).map(String).filter(Boolean);
    if (identityList.length > 0) {
      payload.campaign.identities = identityList;
      payload.session.identities = identityList;
    }

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
    requiresIdentity,
    selectedIdentities,
    selectedStoreId,
    startTime,
    endTime,
    storeId,
  ]);

  if (!open) return null;

  return (
    <Modal open={open} title={GmvMaxTexts.createSeries} onClose={onClose}>
      {optionsQuery.isLoading ? <Loading text="选项加载中…" /> : null}
      <ErrorBlock error={optionsQuery.error} onRetry={optionsQuery.refetch} />

      {step === 1 ? (
        <div className="gmvmax-modal-step">
          <h3>系列信息</h3>
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
            {storeOptions.length === 0 ? <p className="gmvmax-placeholder">暂无可用店铺</p> : null}
          </FormField>
          <FormField label="系列名称">
            <input
              type="text"
              value={form.name}
              onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
              placeholder="请输入系列名称"
            />
          </FormField>
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
            {requiresIdentity && selectedIdentities.size === 0 ? (
              <div className="gmvmax-tip">直播投放需要至少选择一个身份</div>
            ) : null}
          </FormField>
          <FormField label="广告投放类型">
            {shoppingAdsChoices.length > 0 ? (
              <select
                value={form.shoppingAdsType}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, shoppingAdsType: event.target.value }))
                }
              >
                <option value="">选择类型</option>
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
                placeholder="例如 PRODUCT"
              />
            )}
          </FormField>
          <FormField label="优化目标">
            {optimizationGoalChoices.length > 0 ? (
              <select
                value={form.optimizationGoal}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, optimizationGoal: event.target.value }))
                }
              >
                <option value="">选择目标</option>
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
                placeholder="例如 GMV"
              />
            )}
          </FormField>
          <FormField label="出价类型">
            {bidTypeChoices.length > 0 ? (
              <select
                value={form.bidType}
                onChange={(event) => setForm((prev) => ({ ...prev, bidType: event.target.value }))}
              >
                <option value="">选择出价类型</option>
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
                placeholder="出价类型"
              />
            )}
          </FormField>
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
          <div className="gmvmax-modal-grid">
            <FormField label="预算">
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.budget}
                onChange={(event) => setForm((prev) => ({ ...prev, budget: event.target.value }))}
                placeholder="可选"
              />
            </FormField>
            <FormField label="ROAS 出价">
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.roasBid}
                onChange={(event) => setForm((prev) => ({ ...prev, roasBid: event.target.value }))}
                placeholder="可选"
              />
            </FormField>
          </div>
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={onClose}>
              {GmvMaxTexts.cancel}
            </button>
            <button type="button" onClick={goNext} disabled={!canProceedStep1}>
              {GmvMaxTexts.next}
            </button>
          </div>
        </div>
      ) : null}

      {step === 2 ? (
        <div className="gmvmax-modal-step">
          <h3>选择商品</h3>
          <ProductSelectionPanel
            products={productsData}
            selectedIds={localSelectedIds}
            onToggle={toggleProduct}
            onToggleAll={toggleAll}
            onSelectAll={selectAllProducts}
            onClearAll={clearAllProducts}
            storeNames={storeNameById}
            loading={productsLoading || productsQuery.isLoading || productsQuery.isFetching}
            emptyMessage={
              productsQuery.isLoading
                ? '商品加载中…'
                : '该店铺暂无可投放商品。'
            }
          />
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={goBack}>
              {GmvMaxTexts.back}
            </button>
            <button type="button" onClick={goNext} disabled={!canProceedStep2}>
              {GmvMaxTexts.next}
            </button>
          </div>
        </div>
      ) : null}

      {step === 3 ? (
        <div className="gmvmax-modal-step">
          <h3>确认信息</h3>
          <dl className="gmvmax-review-list">
            <div>
              <dt>系列名称</dt>
              <dd>{form.name || '—'}</dd>
            </div>
            <div>
              <dt>店铺</dt>
              <dd>
                {storeOptions.find((item) => item.value === selectedStoreId)?.label || selectedStoreId || '—'}
              </dd>
            </div>
            <div>
              <dt>广告投放类型</dt>
              <dd>{form.shoppingAdsType || '—'}</dd>
            </div>
            <div>
              <dt>优化目标</dt>
              <dd>{form.optimizationGoal || '—'}</dd>
            </div>
            <div>
              <dt>出价类型</dt>
              <dd>{form.bidType || '—'}</dd>
            </div>
            <div>
              <dt>预算</dt>
              <dd>{form.budget ? formatMoney(parseOptionalFloat(form.budget)) : '—'}</dd>
            </div>
            <div>
              <dt>ROAS 出价</dt>
              <dd>{form.roasBid ? formatMoney(parseOptionalFloat(form.roasBid)) : '—'}</dd>
            </div>
            <div>
              <dt>开始时间</dt>
              <dd>{startTime || '—'}</dd>
            </div>
            <div>
              <dt>结束时间</dt>
              <dd>{endTime || '—'}</dd>
            </div>
            <div>
              <dt>身份</dt>
              <dd>
                {selectedIdentities.size > 0
                  ? Array.from(selectedIdentities).join(', ')
                  : '—'}
              </dd>
            </div>
            <div>
              <dt>已选商品</dt>
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
              <li>…以及 {selectedProducts.length - 10} 个更多</li>
            ) : null}
          </ul>
          {submitError ? <div className="gmvmax-error">{submitError}</div> : null}
          {createMutation.isPending ? <Loading text="正在创建系列…" /> : null}
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={goBack} disabled={createMutation.isPending}>
              {GmvMaxTexts.back}
            </button>
            <button type="button" onClick={handleSubmit} disabled={createMutation.isPending}>
              {GmvMaxTexts.createSeries}
            </button>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}

