import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import Modal from '@/components/ui/Modal.jsx';
import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';

import ProductSelectionPanel from './ProductSelectionPanel.jsx';
import {
  ensureArray,
  formatError,
  formatMoney,
  getStoreId,
  getStoreLabel,
  parseOptionalFloat,
} from './helpers.js';
import { ErrorBlock } from './ErrorHandling.jsx';
import {
  useCreateGmvMaxCampaignMutation,
  useGmvMaxOptionsQuery,
  useProductsQuery,
  useGmvMaxIdentitiesQuery,
  useGmvMaxPrecheckMutation,
} from '../../hooks/gmvMaxQueries.js';
import { GmvMaxTexts } from '../../locale.js';

const DEFAULT_PRODUCT_SPECIFIC_TYPE = 'ALL';
const CUSTOM_PRODUCT_SPECIFIC_TYPE = 'CUSTOMIZED_PRODUCTS';
const SCHEDULE_FROM_NOW = 'SCHEDULE_FROM_NOW';
const SCHEDULE_START_END = 'SCHEDULE_START_END';

function getDateInputValue(date) {
  if (!date) return '';
  const parsed = new Date(date);
  if (Number.isNaN(parsed?.getTime?.())) return '';
  return parsed.toISOString().slice(0, 16);
}

function toIsoString(value) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed?.getTime?.())) return '';
  return parsed.toISOString();
}

function sortIds(list) {
  return Array.from(list || []).map(String).filter(Boolean).sort();
}

function normalizeIdentityOption(identity) {
  if (!identity || typeof identity !== 'object') return null;
  const value = String(identity.identity_id || identity.id || identity.identityId || '').trim();
  if (!value) return null;
  const label = identity.identity_name || identity.name || identity.identityName || value;
  return { value, label, data: identity };
}

export default function CreateSeriesModal({
  open,
  onClose,
  workspaceId,
  provider,
  authId,
  advertiserId,
  storeId,
  storeNameById,
  onCreated,
}) {
  const [selectedStoreId, setSelectedStoreId] = useState(storeId ? String(storeId) : '');
  const [productSpecificType, setProductSpecificType] = useState(DEFAULT_PRODUCT_SPECIFIC_TYPE);
  const [selectedItemIds, setSelectedItemIds] = useState(new Set());
  const [selectedIdentities, setSelectedIdentities] = useState(new Set());
  const [campaignName, setCampaignName] = useState('');
  const [budget, setBudget] = useState('');
  const [roasBid, setRoasBid] = useState('');
  const [scheduleType, setScheduleType] = useState(SCHEDULE_FROM_NOW);
  const [scheduleStart, setScheduleStart] = useState(getDateInputValue(new Date()));
  const [scheduleEnd, setScheduleEnd] = useState('');
  const [productSearch, setProductSearch] = useState('');
  const [errors, setErrors] = useState({});
  const [submitError, setSubmitError] = useState(null);
  const [precheckResult, setPrecheckResult] = useState(null);
  const [precheckParams, setPrecheckParams] = useState(null);
  const [precheckError, setPrecheckError] = useState(null);
  const autoPrecheckKeyRef = useRef('');

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
      .map((store) => ({
        value: getStoreId(store),
        label: `${getStoreLabel(store)}${store?.region ? ` · ${store.region}` : ''}`,
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

  useEffect(() => {
    if (!open) return;
    setProductSpecificType(DEFAULT_PRODUCT_SPECIFIC_TYPE);
    setSelectedItemIds(new Set());
    setSelectedIdentities(new Set());
    setCampaignName('');
    setBudget('');
    setRoasBid('');
    setScheduleType(SCHEDULE_FROM_NOW);
    setScheduleStart(getDateInputValue(new Date()));
    setScheduleEnd('');
    setErrors({});
    setSubmitError(null);
    setPrecheckResult(null);
    setPrecheckParams(null);
    setPrecheckError(null);
    autoPrecheckKeyRef.current = '';
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setSelectedItemIds(new Set());
    setPrecheckResult(null);
    setPrecheckParams(null);
    setPrecheckError(null);
  }, [open, selectedStoreId]);

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
      .map(normalizeIdentityOption)
      .filter(Boolean);
  }, [identitiesQuery.data]);

  const identityOptionMap = useMemo(() => {
    const map = new Map();
    identityOptions.forEach((item) => map.set(item.value, item));
    return map;
  }, [identityOptions]);

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    {
      store_id: selectedStoreId || undefined,
      advertiser_id: advertiserId || undefined,
      ad_creation_eligible: 'GMV_MAX',
      status: 'AVAILABLE',
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
    return normalized;
  }, [productsQuery.data]);

  const precheckMutation = useGmvMaxPrecheckMutation(workspaceId, provider, authId);

  const currentPrecheckParams = useMemo(() => {
    if (!selectedStoreId) return null;
    const itemGroupIds =
      productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE ? sortIds(selectedItemIds) : [];
    return {
      store_id: selectedStoreId,
      store_authorized_bc_id:
        selectedStore?.store_authorized_bc_id || selectedStore?.authorized_bc_id || undefined,
      advertiser_id: advertiserId || undefined,
      identity_id: null,
      product_specific_type: productSpecificType,
      item_group_ids: itemGroupIds,
    };
  }, [advertiserId, productSpecificType, selectedItemIds, selectedStore, selectedStoreId]);

  const precheckMatches = useMemo(() => {
    if (!currentPrecheckParams || !precheckParams) return false;
    const currentKey = JSON.stringify({
      ...currentPrecheckParams,
      item_group_ids: sortIds(currentPrecheckParams.item_group_ids || []),
    });
    const previousKey = JSON.stringify({
      ...precheckParams,
      item_group_ids: sortIds(precheckParams.item_group_ids || []),
    });
    return currentKey === previousKey;
  }, [currentPrecheckParams, precheckParams]);

  const performPrecheck = useCallback(async () => {
    if (!currentPrecheckParams) {
      setPrecheckError('请选择店铺后进行预检');
      return null;
    }
    setPrecheckError(null);
    try {
      const result = await precheckMutation.mutateAsync(currentPrecheckParams);
      setPrecheckResult(result);
      setPrecheckParams(currentPrecheckParams);
      return result;
    } catch (error) {
      const message = formatError(error) || '预检失败，请稍后重试';
      setPrecheckError(message);
      setPrecheckResult(null);
      setPrecheckParams(null);
      throw error;
    }
  }, [currentPrecheckParams, precheckMutation]);

  useEffect(() => {
    if (!open) return;
    if (!currentPrecheckParams || currentPrecheckParams.product_specific_type !== DEFAULT_PRODUCT_SPECIFIC_TYPE) {
      return;
    }
    const key = JSON.stringify(currentPrecheckParams);
    if (autoPrecheckKeyRef.current === key) return;
    autoPrecheckKeyRef.current = key;
    performPrecheck();
  }, [currentPrecheckParams, open, performPrecheck]);

  const toggleProduct = useCallback((id) => {
    setSelectedItemIds((prev) => {
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
    setSelectedItemIds((prev) => {
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
    setSelectedItemIds(new Set(normalized));
  }, []);

  const clearAllProducts = useCallback(() => {
    setSelectedItemIds(new Set());
  }, []);

  const handleIdentityChange = useCallback((event) => {
    const values = Array.from(event.target.selectedOptions || []).map((option) => option.value);
    const limited = values.slice(0, 4).map(String);
    setSelectedIdentities(new Set(limited));
  }, []);

  const createMutation = useCreateGmvMaxCampaignMutation(workspaceId, provider, authId);

  const recommendedRoas = precheckResult?.recommended_roas_bid;
  const recommendedBudget = precheckResult?.recommended_budget;
  const currency = selectedStore?.currency || selectedStore?.currency_code || selectedStore?.currencyCode;

  const validationErrors = useMemo(() => {
    const nextErrors = {};
    if (!selectedStoreId) {
      nextErrors.store_id = '请选择店铺';
    }
    if (!campaignName.trim()) {
      nextErrors.campaign_name = '系列名称不能为空';
    }
    if (selectedIdentities.size === 0) {
      nextErrors.identities = '请至少选择1个身份';
    }
    if (selectedIdentities.size > 4) {
      nextErrors.identities = '最多选择4个身份';
    }
    if (productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE && selectedItemIds.size === 0) {
      nextErrors.products = '请选择至少一个商品';
    }
    const budgetValue = parseOptionalFloat(budget);
    if (budgetValue === undefined || Number.isNaN(budgetValue) || budgetValue <= 0) {
      nextErrors.budget = '请输入合法的预算金额';
    }
    const roasValue = parseOptionalFloat(roasBid);
    if (roasValue === undefined || Number.isNaN(roasValue) || roasValue <= 0) {
      nextErrors.roasBid = '请输入合法的 ROAS 出价';
    }
    if (scheduleType === SCHEDULE_START_END) {
      if (!scheduleStart) {
        nextErrors.scheduleStart = '请选择开始时间';
      }
      if (!scheduleEnd) {
        nextErrors.scheduleEnd = '请选择结束时间';
      }
      if (scheduleStart && scheduleEnd) {
        const start = new Date(scheduleStart).getTime();
        const end = new Date(scheduleEnd).getTime();
        if (!Number.isNaN(start) && !Number.isNaN(end) && end <= start) {
          nextErrors.scheduleEnd = '结束时间必须晚于开始时间';
        }
      }
    }
    return nextErrors;
  }, [
    budget,
    campaignName,
    productSpecificType,
    roasBid,
    scheduleEnd,
    scheduleStart,
    scheduleType,
    selectedIdentities.size,
    selectedItemIds.size,
    selectedStoreId,
  ]);

  useEffect(() => {
    setErrors(validationErrors);
  }, [validationErrors]);

  const describeBlockingPrecheckIssue = useCallback(
    (result) => {
      const target = result || precheckResult;
      if (!target) return '';
      if (target.is_gmv_max_available === false) {
        return '当前店铺不支持 Product GMV Max';
      }
      if (target.needs_exclusive_auth) {
        return '当前授权账号与店铺独占授权不匹配';
      }
      if (
        productSpecificType === DEFAULT_PRODUCT_SPECIFIC_TYPE &&
        target.promote_all_products_allowed === false
      ) {
        return '该店铺已有启用的 Product GMV Max 系列，不允许再创建全店系列';
      }
      return '';
    },
    [precheckResult, productSpecificType],
  );

  const hasBlockingPrecheckIssue = useMemo(() => describeBlockingPrecheckIssue(), [
    describeBlockingPrecheckIssue,
  ]);

  const primaryErrorMessage = useMemo(() => {
    const keyOrder = [
      'store_id',
      'campaign_name',
      'identities',
      'products',
      'budget',
      'roasBid',
      'scheduleStart',
      'scheduleEnd',
    ];
    const keys = Object.keys(errors || {});
    if (keys.length === 0) return '';
    const prioritizedKey = keyOrder.find((key) => keys.includes(key));
    if (prioritizedKey && errors[prioritizedKey]) return errors[prioritizedKey];
    const firstKey = keys[0];
    return errors[firstKey] || '请先修正表单错误';
  }, [errors]);

  const disableCreateReason = useMemo(() => {
    if (Object.keys(errors).length > 0) return primaryErrorMessage || '请先修正表单错误';
    if (!precheckMatches) return '请先完成预检';
    if (hasBlockingPrecheckIssue) return hasBlockingPrecheckIssue;
    return '';
  }, [errors, hasBlockingPrecheckIssue, precheckMatches, primaryErrorMessage]);

  const handleUseRecommended = useCallback(() => {
    if (!precheckMatches || precheckResult?.recommended_roas_bid === undefined) return;
    setRoasBid(String(precheckResult.recommended_roas_bid));
    if (precheckResult.recommended_budget !== undefined && precheckResult.recommended_budget !== null) {
      setBudget(String(precheckResult.recommended_budget));
    }
  }, [precheckMatches, precheckResult]);

  const buildIdentityList = useCallback(() => {
    return Array.from(selectedIdentities).map((id) => {
      const option = identityOptionMap.get(id);
      const data = option?.data || {};
      return {
        identity_id: id,
        identity_type: data.identity_type || data.identityType || null,
        identity_authorized_bc_id:
          data.identity_authorized_bc_id || data.identityAuthorizedBcId || data.authorized_bc_id || null,
        store_id: selectedStoreId,
      };
    });
  }, [identityOptionMap, selectedIdentities, selectedStoreId]);

  const handleSubmit = useCallback(async () => {
    setSubmitError(null);
    const latestErrors = Object.keys(validationErrors).length > 0 ? validationErrors : errors;
    if (Object.keys(latestErrors).length > 0) {
      setErrors(latestErrors);
      return;
    }
    let latestPrecheckResult = precheckResult;
    try {
      if (!precheckMatches) {
        latestPrecheckResult = await performPrecheck();
      }
    } catch {
      return;
    }

    const blockingIssue = describeBlockingPrecheckIssue(latestPrecheckResult);
    if (!latestPrecheckResult || blockingIssue) {
      setErrors((prev) => ({ ...prev }));
      setSubmitError(blockingIssue || '预检未通过');
      return;
    }

    const payload = {
      store_id: selectedStoreId,
      campaign_name: campaignName.trim(),
      product_specific_type: productSpecificType,
      item_group_ids:
        productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE ? sortIds(selectedItemIds) : undefined,
      roas_bid: parseOptionalFloat(roasBid),
      budget: parseOptionalFloat(budget),
      schedule_type: scheduleType,
      schedule_start_time: toIsoString(scheduleStart),
      schedule_end_time: toIsoString(scheduleEnd) || null,
      product_video_specific_type: 'AUTO_SELECTION',
      identity_list: buildIdentityList(),
      advertiser_id: advertiserId ? String(advertiserId) : undefined,
    };

    try {
      await createMutation.mutateAsync(payload);
      onCreated?.();
    } catch (error) {
      setSubmitError(formatError(error));
    }
  }, [
    budget,
    buildIdentityList,
    campaignName,
    createMutation,
    errors,
    describeBlockingPrecheckIssue,
    onCreated,
    performPrecheck,
    precheckMatches,
    productSpecificType,
    roasBid,
    scheduleEnd,
    scheduleStart,
    scheduleType,
    selectedItemIds,
    selectedStoreId,
    advertiserId,
    validationErrors,
  ]);

  if (!open) return null;

  const occupancyInfo = precheckResult
    ? {
        available: (precheckResult.unoccupied_item_group_ids || []).length,
        occupied: (precheckResult.occupied_item_group_ids || []).length,
      }
    : null;

  return (
    <Modal open={open} title={GmvMaxTexts.createProductSeries} onClose={onClose}>
      {optionsQuery.isLoading ? <Loading text="选项加载中…" /> : null}
      <ErrorBlock error={optionsQuery.error} onRetry={optionsQuery.refetch} />

      {hasBlockingPrecheckIssue ? (
        <div className="gmvmax-banner gmvmax-banner--error">{hasBlockingPrecheckIssue}</div>
      ) : precheckResult?.needs_exclusive_auth ? (
        <div className="gmvmax-banner gmvmax-banner--warning">
          当前授权账号与店铺独占授权不匹配，请切换正确的授权账户。
        </div>
      ) : null}
      {precheckError ? <div className="gmvmax-banner gmvmax-banner--error">{precheckError}</div> : null}

      <section className="gmvmax-section">
        <h3>基础信息</h3>
        <FormField label="店铺" error={errors.store_id}>
          <select
            value={selectedStoreId}
            onChange={(event) => setSelectedStoreId(event.target.value)}
            disabled={storeOptions.length === 0}
          >
            <option value="">请选择店铺</option>
            {storeOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          {storeOptions.length === 0 ? <p className="gmvmax-placeholder">暂无可用店铺</p> : null}
        </FormField>

        <FormField label="系列名称" error={errors.campaign_name}>
          <input
            type="text"
            value={campaignName}
            onChange={(event) => setCampaignName(event.target.value)}
            maxLength={128}
            placeholder="请输入系列名称，例如：US 店铺 GMV Max Q4 测试"
          />
        </FormField>

        <div className="gmvmax-inline-tags">
          <span className="gmvmax-tag" aria-label="广告投放类型">
            广告投放类型：PRODUCT
          </span>
          <span className="gmvmax-tag" aria-label="优化目标">
            优化目标：VALUE（GMV）
          </span>
          <span className="gmvmax-tag" aria-label="出价类型">
            出价类型：VO_MIN_ROAS
          </span>
        </div>
      </section>

      <section className="gmvmax-section">
        <h3>推广范围 & 身份</h3>
        <FormField label="推广范围">
          <div className="gmvmax-radio-group">
            <label>
              <input
                type="radio"
                name="product_specific_type"
                value={DEFAULT_PRODUCT_SPECIFIC_TYPE}
                checked={productSpecificType === DEFAULT_PRODUCT_SPECIFIC_TYPE}
                onChange={() => setProductSpecificType(DEFAULT_PRODUCT_SPECIFIC_TYPE)}
              />
              推广店铺所有商品
            </label>
            <label>
              <input
                type="radio"
                name="product_specific_type"
                value={CUSTOM_PRODUCT_SPECIFIC_TYPE}
                checked={productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE}
                onChange={() => setProductSpecificType(CUSTOM_PRODUCT_SPECIFIC_TYPE)}
              />
              只推广选定商品
            </label>
          </div>
          {productSpecificType === DEFAULT_PRODUCT_SPECIFIC_TYPE &&
          precheckResult?.promote_all_products_allowed === false ? (
            <div className="gmvmax-error-text">
              该店铺已有启用的 Product GMV Max 系列，不允许再创建全店系列。
            </div>
          ) : null}
        </FormField>

        {productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE ? (
          <div className="gmvmax-card">
            {occupancyInfo ? (
              <div className="gmvmax-tip">
                当前店铺可用于 GMV Max 的商品：{occupancyInfo.available} 个可用 / {occupancyInfo.occupied} 个已占用
              </div>
            ) : null}
            <ProductSelectionPanel
              products={productsData}
              selectedIds={selectedItemIds}
              onToggle={toggleProduct}
              onToggleAll={toggleAll}
              onSelectAll={selectAllProducts}
              onClearAll={clearAllProducts}
              storeNames={storeNameById}
              loading={productsQuery.isLoading || productsQuery.isFetching}
              emptyMessage={productsQuery.isLoading ? '商品加载中…' : '该店铺暂无可投放商品。'}
              searchTerm={productSearch}
              onSearchChange={setProductSearch}
            />
            {errors.products ? <div className="gmvmax-error-text">{errors.products}</div> : null}
          </div>
        ) : null}

        <FormField label="身份（最多 4 个）" error={errors.identities}>
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
          {selectedIdentities.size > 0 ? (
            <div className="gmvmax-chips">
              {Array.from(selectedIdentities).map((id) => (
                <span key={id} className="gmvmax-chip">
                  {identityOptionMap.get(id)?.label || id}
                </span>
              ))}
            </div>
          ) : null}
        </FormField>

        <FormField label="创意模式">
          <div className="gmvmax-radio-group">
            <label>
              <input type="radio" checked readOnly /> 系统自动选择视频
            </label>
            <label>
              <input type="radio" disabled /> 自定义视频（即将推出）
            </label>
          </div>
        </FormField>
      </section>

      <section className="gmvmax-section">
        <h3>出价 & 排期</h3>
        <FormField label="预算" error={errors.budget}>
          <div className="gmvmax-inline-input">
            <input
              type="number"
              min="0"
              value={budget}
              onChange={(event) => setBudget(event.target.value)}
              placeholder="请输入预算"
            />
            <span className="gmvmax-plain-text">{currency || '店铺币种'}</span>
          </div>
          <div className="gmvmax-tip">单位：店铺币种</div>
        </FormField>

        <FormField label="ROAS 出价" error={errors.roasBid}>
          <div className="gmvmax-inline-input">
            <input
              type="number"
              min="0"
              value={roasBid}
              onChange={(event) => setRoasBid(event.target.value)}
              placeholder="请输入 ROAS 出价"
            />
            <button type="button" onClick={handleUseRecommended} disabled={!precheckMatches || recommendedRoas === undefined}>
              使用推荐值
            </button>
          </div>
          {precheckMatches && (recommendedRoas !== undefined || recommendedBudget !== undefined) ? (
            <div className="gmvmax-tip">
              推荐出价：{recommendedRoas ?? '—'}，推荐预算：
              {recommendedBudget !== undefined ? formatMoney(recommendedBudget) : '—'}
            </div>
          ) : (
            <div className="gmvmax-tip">请先完成预检以获取推荐出价</div>
          )}
        </FormField>

        <FormField label="排期类型">
          <div className="gmvmax-radio-group">
            <label>
              <input
                type="radio"
                name="schedule_type"
                value={SCHEDULE_FROM_NOW}
                checked={scheduleType === SCHEDULE_FROM_NOW}
                onChange={() => {
                  setScheduleType(SCHEDULE_FROM_NOW);
                  setScheduleStart(getDateInputValue(new Date()));
                }}
              />
              立即开始，长期运行
            </label>
            <label>
              <input
                type="radio"
                name="schedule_type"
                value={SCHEDULE_START_END}
                checked={scheduleType === SCHEDULE_START_END}
                onChange={() => setScheduleType(SCHEDULE_START_END)}
              />
              指定开始和结束时间
            </label>
          </div>
        </FormField>

        <div className="gmvmax-modal-grid">
          <FormField label="开始时间" error={errors.scheduleStart}>
            <input
              type="datetime-local"
              value={scheduleStart}
              onChange={(event) => setScheduleStart(event.target.value)}
              disabled={scheduleType === SCHEDULE_FROM_NOW}
            />
          </FormField>
          <FormField label="结束时间" error={errors.scheduleEnd}>
            <input
              type="datetime-local"
              value={scheduleEnd}
              onChange={(event) => setScheduleEnd(event.target.value)}
            />
          </FormField>
        </div>
      </section>

      <div className="gmvmax-precheck-actions">
        <button type="button" onClick={performPrecheck} disabled={!selectedStoreId || precheckMutation.isPending}>
          {precheckMutation.isPending ? '预检中…' : '重新预检'}
        </button>
        {!precheckMatches && <span className="gmvmax-tip">请预检后再创建系列</span>}
      </div>

      {submitError ? <div className="gmvmax-error">{submitError}</div> : null}
      {createMutation.isPending ? <Loading text="正在创建系列…" /> : null}

      <div className="gmvmax-modal-footer">
        <button type="button" onClick={onClose} disabled={createMutation.isPending}>
          {GmvMaxTexts.cancel}
        </button>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={createMutation.isPending || Boolean(disableCreateReason)}
          title={disableCreateReason || ''}
        >
          {GmvMaxTexts.createSeries}
        </button>
      </div>
    </Modal>
  );
}
