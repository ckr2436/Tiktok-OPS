import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

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
  getStoreLabel,
} from './helpers.js';
import {
  useUpdateGmvMaxCampaignMutation,
  useGmvMaxIdentitiesQuery,
  useGmvMaxPrecheckMutation,
  useProductsQuery,
} from '../../hooks/gmvMaxQueries.js';
import { GmvMaxTexts } from '../../locale.js';

const IDENTITY_LIMIT = 50;

function toDateTimeInputValue(value) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed?.getTime?.())) {
    return String(value).replace(' ', 'T').slice(0, 16);
  }
  return parsed.toISOString().slice(0, 16);
}

function sortIds(list) {
  return Array.from(list || []).map(String).filter(Boolean).sort();
}

function normalizeIdentityOption(identity) {
  if (!identity || typeof identity !== 'object') return null;
  const value = String(identity.identity_id || identity.id || identity.identityId || '').trim();
  if (!value) return null;
  const label =
    identity.user_name ||
    identity.identity_name ||
    identity.name ||
    identity.identityName ||
    identity.display_name ||
    value;
  return { value, label, data: identity };
}

function getIdentityAvatar(identity) {
  const data = identity?.data || identity || {};
  return data.profile_image || data.profileImage || data.avatar_url || data.avatarUrl || '';
}

function getIdentityInitial(label, value) {
  const text = String(label || value || '').trim();
  return text ? text.slice(0, 1).toUpperCase() : 'I';
}

function parseMaybeJson(value) {
  if (!value || typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function addProducts(target, list) {
  ensureArray(list).forEach((product) => {
    if (!product || typeof product !== 'object') return;
    const id = getProductIdentifier(product);
    if (!id || target.has(id)) return;
    target.set(id, product);
  });
}

function addProductIds(target, list) {
  ensureArray(list).forEach((item) => {
    const id =
      item && typeof item === 'object'
        ? getProductIdentifier(item) || item.item_group_id || item.itemGroupId
        : item;
    if (id === undefined || id === null || String(id).trim() === '') return;
    const key = String(id);
    if (!target.has(key)) {
      target.set(key, {
        item_group_id: key,
        product_id: key,
        title: `商品 ${key}`,
        gmv_max_ads_status: 'IN_CAMPAIGN',
        status: 'AVAILABLE',
      });
    }
  });
}

function collectCampaignProducts(detail, campaign) {
  const map = new Map();
  const root = detail || {};
  const campaignDetail = root.campaign || campaign || {};
  addProducts(map, root.products);
  addProducts(map, root.product_list);
  addProducts(map, campaignDetail.products);
  addProducts(map, campaignDetail.product_list);

  ensureArray(root.sessions || root.session_list).forEach((session) => {
    addProducts(map, session?.product_list);
    addProducts(map, session?.products);
  });

  const rawSources = [
    campaignDetail.detail_raw_json,
    campaignDetail.raw_json,
    campaignDetail.rawJson,
    parseMaybeJson(campaignDetail.detail_raw_json),
    parseMaybeJson(campaignDetail.raw_json),
  ].filter(Boolean);

  [
    campaignDetail.item_group_ids,
    campaignDetail.itemGroupIds,
    campaignDetail.item_group_id_list,
    campaignDetail.itemGroupIdList,
    root.item_group_ids,
    root.itemGroupIds,
  ].forEach((list) => addProductIds(map, list));

  rawSources.forEach((raw) => {
    addProducts(map, raw.products);
    addProducts(map, raw.product_list);
    addProductIds(map, raw.item_group_ids);
    addProductIds(map, raw.itemGroupIds);
  });

  return Array.from(map.values());
}

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
  const [productSearch, setProductSearch] = useState('');
  const [submitError, setSubmitError] = useState(null);

  const campaignId = campaign?.campaign_id || campaign?.id || '';
  const detailCampaign = detail?.campaign || campaign || {};

  const detailProducts = useMemo(
    () => collectCampaignProducts(detail, campaign),
    [campaign, detail],
  );

  const initialProductSet = useMemo(() => {
    const ids = new Set();
    detailProducts.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) ids.add(id);
    });
    return ids;
  }, [detailProducts]);

  const campaignStoreBcId = useMemo(() => {
    const store = detailCampaign?.store || {};
    return (
      detailCampaign?.store_authorized_bc_id ||
      detailCampaign?.authorized_bc_id ||
      detailCampaign?.bc_id ||
      store.store_authorized_bc_id ||
      store.bc_id ||
      undefined
    );
  }, [detailCampaign]);

  const initialIdentities = useMemo(() => {
    const list = ensureArray(
      detailCampaign?.identities || detailCampaign?.identity_list || detailCampaign?.identityList,
    )
      .map((item) => (typeof item === 'object' ? item.identity_id || item.id : item))
      .filter(Boolean)
      .map(String);
    return new Set(list);
  }, [detailCampaign]);

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    {
      store_id: selectedStoreId || storeId || undefined,
      advertiser_id: detailCampaign?.advertiser_id || detailCampaign?.advertiserId || undefined,
      owner_bc_id: campaignStoreBcId,
      gmv_max_ads_status: 'UNOCCUPIED',
      ad_creation_eligible: 'GMV_MAX',
      status: 'AVAILABLE',
      page_size: 500,
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
      if (id) map.set(id, product);
    });
    queried.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id && !map.has(id)) map.set(id, product);
    });
    detailProducts.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id && !map.has(id)) map.set(id, product);
    });
    return Array.from(map.values());
  }, [detailProducts, products, productsQuery.data]);

  const availableProductIds = useMemo(() => getAvailableProductIds(mergedProducts), [mergedProducts]);

  const identityParams = useMemo(
    () => ({
      store_id: selectedStoreId || storeId || undefined,
      advertiser_id: detailCampaign?.advertiser_id || detailCampaign?.advertiserId || undefined,
      store_authorized_bc_id:
        detailCampaign?.store_authorized_bc_id || detailCampaign?.authorized_bc_id || undefined,
    }),
    [detailCampaign, selectedStoreId, storeId],
  );

  const identitiesQuery = useGmvMaxIdentitiesQuery(workspaceId, provider, authId, identityParams, {
    enabled: Boolean(open && workspaceId && provider && authId && (selectedStoreId || storeId)),
  });

  const identityOptions = useMemo(() => {
    const payload = identitiesQuery.data || {};
    const list = ensureArray(payload.identities || payload.identity_list || payload.items);
    return list.map(normalizeIdentityOption).filter(Boolean);
  }, [identitiesQuery.data]);

  const identityOptionMap = useMemo(() => {
    const map = new Map();
    identityOptions.forEach((item) => map.set(item.value, item));
    return map;
  }, [identityOptions]);

  const precheckMutation = useGmvMaxPrecheckMutation(workspaceId, provider, authId);
  const [identityPrecheck, setIdentityPrecheck] = useState(null);
  const identityPrecheckKeyRef = useRef('');

  useEffect(() => {
    if (!open || !selectedStoreId || identityOptions.length > 0 || precheckMutation.isPending) return;
    const storeAuthorizedBcId =
      detailCampaign?.store_authorized_bc_id ||
      detailCampaign?.authorized_bc_id ||
      detailCampaign?.detail_raw_json?.store_authorized_bc_id ||
      detailCampaign?.raw_json?.store_authorized_bc_id ||
      undefined;
    if (!storeAuthorizedBcId) return;
    const key = `${selectedStoreId}:${campaignId}:${storeAuthorizedBcId}:${Array.from(initialProductSet).join(',')}`;
    if (identityPrecheckKeyRef.current === key) return;
    identityPrecheckKeyRef.current = key;
    precheckMutation
      .mutateAsync({
        store_id: String(selectedStoreId),
        store_authorized_bc_id: String(storeAuthorizedBcId),
        advertiser_id: detailCampaign?.advertiser_id || detailCampaign?.advertiserId || undefined,
        product_specific_type: initialProductSet.size > 0 ? 'CUSTOMIZED_PRODUCTS' : 'ALL',
        item_group_ids: Array.from(initialProductSet),
      })
      .then((result) => setIdentityPrecheck(result))
      .catch(() => setIdentityPrecheck(null));
  }, [
    authId,
    campaignId,
    detailCampaign,
    identityOptions.length,
    initialProductSet,
    open,
    precheckMutation,
    provider,
    selectedStoreId,
    workspaceId,
  ]);

  const precheckIdentityOptions = useMemo(
    () => ensureArray(identityPrecheck?.available_identities).map(normalizeIdentityOption).filter(Boolean),
    [identityPrecheck],
  );

  const visibleIdentityOptions = useMemo(() => {
    if (identityOptions.length > 0) return identityOptions;
    if (precheckIdentityOptions.length > 0) return precheckIdentityOptions;
    return Array.from(initialIdentities).map((id) => ({
      value: String(id),
      label: `已绑定身份 ${id}`,
      data: {},
    }));
  }, [identityOptions, precheckIdentityOptions, initialIdentities]);

  const identitySelectionLocked =
    identityOptions.length === 0 && precheckIdentityOptions.length === 0 && visibleIdentityOptions.length > 0;

  const storeOptions = useMemo(() => {
    const options = [];
    if (storeNameById && typeof storeNameById.forEach === 'function') {
      storeNameById.forEach((label, id) => {
        if (id) options.push({ value: String(id), label: label || String(id) });
      });
    }
    const campaignStoreId = detailCampaign?.store_id || detailCampaign?.storeId || storeId;
    if (campaignStoreId && !options.find((item) => item.value === String(campaignStoreId))) {
      options.push({
        value: String(campaignStoreId),
        label: getStoreLabel(detailCampaign?.store || {}) || String(campaignStoreId),
      });
    }
    return options;
  }, [detailCampaign, storeId, storeNameById]);

  useEffect(() => {
    if (!open || !detail) return;
    setName(detailCampaign?.campaign_name || detailCampaign?.name || '');
    setBudget(
      detailCampaign?.budget !== undefined && detailCampaign?.budget !== null
        ? String(detailCampaign.budget)
        : '',
    );
    setRoasBid(
      detailCampaign?.roas_bid !== undefined && detailCampaign?.roas_bid !== null
        ? String(detailCampaign.roas_bid)
        : '',
    );
    setSelectedStoreId(String(detailCampaign?.store_id || detailCampaign?.storeId || storeId || ''));
    setStartTime(toDateTimeInputValue(detailCampaign?.start_time || detailCampaign?.startTime));
    setEndTime(toDateTimeInputValue(detailCampaign?.end_time || detailCampaign?.endTime));
    setSelectedIdentities(new Set(Array.from(initialIdentities)));
    setLocalSelectedIds(new Set(initialProductSet));
    setProductSearch('');
    setSubmitError(null);
  }, [detail, detailCampaign, initialIdentities, initialProductSet, open, storeId]);

  useEffect(() => {
    if (!open || selectedIdentities.size > 0 || identityOptions.length === 0) return;
    setSelectedIdentities(new Set(identityOptions.slice(0, IDENTITY_LIMIT).map((item) => item.value)));
  }, [identityOptions, open, selectedIdentities.size]);

  useEffect(() => {
    if (!open) return;
    setLocalSelectedIds((prev) => {
      const next = new Set(prev);
      initialProductSet.forEach((id) => next.add(id));
      return next;
    });
  }, [initialProductSet, open]);

  useEffect(() => {
    if (!open || availableProductIds.size === 0) return;
    setLocalSelectedIds((prev) => {
      const next = new Set();
      prev.forEach((id) => {
        if (availableProductIds.has(id) || initialProductSet.has(id)) next.add(id);
      });
      return next;
    });
  }, [availableProductIds, initialProductSet, open]);

  const toggleProduct = useCallback((id) => {
    setLocalSelectedIds((prev) => {
      const next = new Set(prev);
      const key = String(id);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const toggleAll = useCallback((ids) => {
    setLocalSelectedIds((prev) => {
      const next = new Set(prev);
      const normalized = (ids || []).map(String);
      const shouldDeselect = normalized.every((id) => next.has(id));
      if (shouldDeselect) normalized.forEach((id) => next.delete(id));
      else normalized.forEach((id) => next.add(id));
      return next;
    });
  }, []);

  const toggleIdentity = useCallback((id) => {
    setSelectedIdentities((prev) => {
      const next = new Set(prev);
      const key = String(id);
      if (next.has(key)) next.delete(key);
      else if (next.size < IDENTITY_LIMIT) next.add(key);
      return next;
    });
  }, []);

  const updateCampaignMutation = useUpdateGmvMaxCampaignMutation(workspaceId, provider, authId, campaignId);

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

  const buildIdentityList = useCallback(() => {
    return identityList.map((id) => {
      const data = identityOptionMap.get(id)?.data || {};
      return {
        identity_id: id,
        identity_type: data.identity_type || data.identityType || null,
        identity_authorized_bc_id:
          data.identity_authorized_bc_id || data.identityAuthorizedBcId || data.authorized_bc_id || null,
        store_id: selectedStoreId || storeId || undefined,
      };
    });
  }, [identityList, identityOptionMap, selectedStoreId, storeId]);

  const handleSubmit = useCallback(async () => {
    if (!campaignId) return;
    const trimmedName = name.trim();
    const campaignPatch = {};
    if (trimmedName && trimmedName !== detailCampaign?.campaign_name) {
      campaignPatch.name = trimmedName;
    }
    const budgetValue = parseOptionalFloat(budget);
    if (budgetValue !== undefined && budgetValue !== Number(detailCampaign?.budget)) {
      campaignPatch.daily_budget = budgetValue;
    }
    const roasValue = parseOptionalFloat(roasBid);
    if (roasValue !== undefined && roasValue !== Number(detailCampaign?.roas_bid)) {
      campaignPatch.roas_bid = roasValue;
    }
    if (startTime && startTime !== toDateTimeInputValue(detailCampaign?.start_time || detailCampaign?.startTime)) {
      campaignPatch.start_time = startTime;
    }
    if (endTime !== toDateTimeInputValue(detailCampaign?.end_time || detailCampaign?.endTime)) {
      campaignPatch.end_time = endTime || null;
    }
    if (productsChanged) {
      campaignPatch.item_group_ids = sortIds(localSelectedIds);
    }
    if (identitiesChanged && identityOptions.length > 0) {
      campaignPatch.identity_list = buildIdentityList();
    }

    setSubmitError(null);
    try {
      if (Object.keys(campaignPatch).length === 0) {
        onClose?.();
        return;
      }
      await updateCampaignMutation.mutateAsync(campaignPatch);
      onUpdated?.();
      onClose?.();
    } catch (error) {
      setSubmitError(formatError(error));
    }
  }, [
    buildIdentityList,
    budget,
    campaignId,
    detailCampaign,
    endTime,
    identitiesChanged,
    identityOptions.length,
    localSelectedIds,
    name,
    onClose,
    onUpdated,
    productsChanged,
    roasBid,
    startTime,
    updateCampaignMutation,
  ]);

  const isSaving = updateCampaignMutation.isPending;
  const startChanged = Boolean(
    startTime && startTime !== toDateTimeInputValue(detailCampaign?.start_time || detailCampaign?.startTime),
  );
  const endChanged = endTime !== toDateTimeInputValue(detailCampaign?.end_time || detailCampaign?.endTime);
  const canSubmit =
    Boolean(detail) &&
    (productsChanged ||
      identitiesChanged ||
      (name.trim() && name.trim() !== detailCampaign?.campaign_name) ||
      (budget && parseOptionalFloat(budget) !== Number(detailCampaign?.budget)) ||
      (roasBid && parseOptionalFloat(roasBid) !== Number(detailCampaign?.roas_bid)) ||
      startChanged ||
      endChanged);

  if (!open) return null;

  return (
    <Modal open={open} title={GmvMaxTexts.editSeries || '编辑系列'} onClose={onClose}>
      {detailLoading ? <Loading text="系列加载中..." /> : null}
      <ErrorBlock error={detailError} onRetry={onRetryDetail} />
      {!detailLoading && !detailError && !detail ? <p>无法获取系列详情。</p> : null}
      {!detail || detailLoading || detailError ? null : (
        <div className="gmvmax-modal-step">
          <section className="gmvmax-section">
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
              <input type="text" value={name} onChange={(event) => setName(event.target.value)} maxLength={128} />
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
            <div className="gmvmax-inline-tags">
              <span className="gmvmax-tag">优化目标：{detailCampaign?.optimization_goal || '-'}</span>
              <span className="gmvmax-tag">广告投放类型：{detailCampaign?.shopping_ads_type || '-'}</span>
              <span className="gmvmax-tag">出价类型：{detailCampaign?.bid_type || 'VO_MIN_ROAS'}</span>
            </div>
          </section>

          <section className="gmvmax-section">
            <h3>身份</h3>
            {identitiesQuery.isLoading ? <Loading text="身份加载中..." /> : null}
            {visibleIdentityOptions.length === 0 ? (
              <p className="gmvmax-placeholder">暂无可用身份</p>
            ) : (
              <div className="gmvmax-check-list">
                {visibleIdentityOptions.map((identity) => (
                  <label key={identity.value} className="gmvmax-check-item">
                    <input
                      type="checkbox"
                      checked={selectedIdentities.has(identity.value)}
                      onChange={() => toggleIdentity(identity.value)}
                      disabled={
                        isSaving ||
                        identitySelectionLocked ||
                        (!selectedIdentities.has(identity.value) && selectedIdentities.size >= IDENTITY_LIMIT)
                      }
                    />
                    <span className="gmvmax-identity-option">
                      <span className="gmvmax-identity-avatar" aria-hidden="true">
                        {getIdentityAvatar(identity) ? (
                          <img src={getIdentityAvatar(identity)} alt="" loading="lazy" />
                        ) : (
                          getIdentityInitial(identity.label, identity.value)
                        )}
                      </span>
                      <span className="gmvmax-identity-text">
                        <strong>{identity.label || identity.value}</strong>
                        <small>{identity.value}</small>
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            )}
            <div className="gmvmax-tip">
              已选 {selectedIdentities.size} / {visibleIdentityOptions.length} 个身份
              {identitySelectionLocked ? '；候选身份暂未返回，保持当前绑定不变。' : ''}
            </div>
          </section>

          <section className="gmvmax-section">
            <h3>商品</h3>
            <ProductSelectionPanel
              products={mergedProducts}
              selectedIds={localSelectedIds}
              onToggle={toggleProduct}
              onToggleAll={toggleAll}
              onSelectAll={(ids) => setLocalSelectedIds(new Set((ids || []).map(String)))}
              onClearAll={() => setLocalSelectedIds(new Set())}
              storeNames={storeNameById}
              loading={productsLoading || productsQuery.isLoading || productsQuery.isFetching}
              emptyMessage={productsQuery.isLoading ? '商品加载中...' : '未找到可投放商品。'}
              disabled={isSaving}
              searchTerm={productSearch}
              onSearchChange={setProductSearch}
            />
          </section>

          {submitError ? <div className="gmvmax-error">{submitError}</div> : null}
          {isSaving ? <Loading text="保存修改中..." /> : null}
          <div className="gmvmax-modal-footer">
            <button
              type="button"
              className="gmvmax-button gmvmax-button--ghost"
              onClick={onClose}
              disabled={isSaving}
            >
              {GmvMaxTexts.cancel || '取消'}
            </button>
            <button
              type="button"
              className="gmvmax-button gmvmax-button--primary"
              onClick={handleSubmit}
              disabled={isSaving || !canSubmit}
            >
              {GmvMaxTexts.saveChanges || '保存修改'}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
