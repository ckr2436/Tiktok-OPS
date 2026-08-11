import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import Modal from '@/components/ui/Modal.jsx';
import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';

import ProductSelectionPanel from './ProductSelectionPanel.jsx';
import {
  ensureArray,
  formatError,
  formatMoney,
  getProductIdentifier,
  getStoreId,
  getStoreLabel,
  collectStoreBusinessCenterCandidates,
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
import {
  buildCampaignCreateIntentKey,
  clearCampaignCreateIntent,
  finalizeCampaignCreateIntent,
  getFinalizedCampaignCreatePayload,
  getOrCreateCampaignCreateIntent,
  getStoredCampaignCreateIntent,
  isDefinitiveCreateRejection,
} from '../../utils/campaignCreateIntent.js';

const DEFAULT_PRODUCT_SPECIFIC_TYPE = 'ALL';
const CUSTOM_PRODUCT_SPECIFIC_TYPE = 'CUSTOMIZED_PRODUCTS';
const SCHEDULE_FROM_NOW = 'SCHEDULE_FROM_NOW';
const SCHEDULE_START_END = 'SCHEDULE_START_END';
const IDENTITY_LIMIT = 20;
const SERIES_CREATE_INTENT_PRODUCT_ID = 'series-modal';
const SERIES_CREATE_INTENT_LAUNCH_MODE = 'SMART_SERIES';

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
  const label = identity.identity_name || identity.name || identity.identityName || identity.user_name || value;
  return { value, label, data: identity };
}

function getRecommendedValue(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return undefined;
}

function getProductPrice(product) {
  if (!product || typeof product !== 'object') return undefined;
  return getRecommendedValue(
    product.effective_price,
    product.effectivePrice,
    product.sale_price,
    product.salePrice,
    product.min_price,
    product.minPrice,
    product.price,
  );
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
  const [effectivePrice, setEffectivePrice] = useState('');
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
  const autoSelectedProductsRef = useRef(false);
  const campaignCreateIntentFallbackRef = useRef(new Map());
  const seriesCreateIntentStorageKey = selectedStoreId
    ? buildCampaignCreateIntentKey({
        workspaceId,
        provider,
        authId,
        advertiserId,
        storeId: selectedStoreId,
        productId: SERIES_CREATE_INTENT_PRODUCT_ID,
        launchMode: SERIES_CREATE_INTENT_LAUNCH_MODE,
      })
    : null;
  const pendingSeriesCreateIntent = seriesCreateIntentStorageKey
    ? getStoredCampaignCreateIntent({
        storageKey: seriesCreateIntentStorageKey,
        fallbackIntents: campaignCreateIntentFallbackRef.current,
      })
    : null;
  const hasPendingSeriesCreatePayload = Boolean(
    getFinalizedCampaignCreatePayload(pendingSeriesCreateIntent),
  );

  const optionsQuery = useGmvMaxOptionsQuery(workspaceId, provider, authId, {}, {
    enabled: Boolean(open && workspaceId && authId),
  });

  const storeOptions = useMemo(() => {
    const payload = optionsQuery.data || {};
    const rawStores = ensureArray(payload.stores || payload.store_list || payload.storeList);
    return rawStores
      .map((store) => ({
        value: getStoreId(store),
        label: getStoreLabel(store),
        data: store,
      }))
      .filter((option) => option.value);
  }, [optionsQuery.data]);

  useEffect(() => {
    if (!open || selectedStoreId) return;
    if (storeOptions.length > 0) setSelectedStoreId(storeOptions[0].value);
  }, [open, selectedStoreId, storeOptions]);

  const selectedStore = useMemo(
    () => storeOptions.find((option) => option.value === selectedStoreId)?.data || null,
    [selectedStoreId, storeOptions],
  );

  const selectedStoreLabel = useMemo(() => {
    if (!selectedStoreId) return '';
    return storeOptions.find((option) => option.value === selectedStoreId)?.label || storeNameById?.get?.(selectedStoreId) || selectedStoreId;
  }, [selectedStoreId, storeNameById, storeOptions]);

  const storeAuthorizedBcId = useMemo(
    () => selectedStore?.store_authorized_bc_id || selectedStore?.authorized_bc_id || undefined,
    [selectedStore],
  );

  const storeBusinessCenterId = useMemo(() => {
    const candidates = collectStoreBusinessCenterCandidates(selectedStore);
    return candidates.length > 0 ? candidates[0] : undefined;
  }, [selectedStore]);

  useEffect(() => {
    if (!open) return;
    setSelectedStoreId(storeId ? String(storeId) : '');
    setProductSpecificType(DEFAULT_PRODUCT_SPECIFIC_TYPE);
    setSelectedItemIds(new Set());
    setSelectedIdentities(new Set());
    setCampaignName('');
    setBudget('');
    setRoasBid('');
    setEffectivePrice('');
    setScheduleType(SCHEDULE_FROM_NOW);
    setScheduleStart(getDateInputValue(new Date()));
    setScheduleEnd('');
    setProductSearch('');
    setErrors({});
    setSubmitError(null);
    setPrecheckResult(null);
    setPrecheckParams(null);
    setPrecheckError(null);
    autoPrecheckKeyRef.current = '';
    autoSelectedProductsRef.current = false;
  }, [open, storeId]);

  useEffect(() => {
    if (!open) return;
    setSelectedItemIds(new Set());
    setSelectedIdentities(new Set());
    setPrecheckResult(null);
    setPrecheckParams(null);
    setPrecheckError(null);
    autoPrecheckKeyRef.current = '';
    autoSelectedProductsRef.current = false;
  }, [open, selectedStoreId]);

  useEffect(() => {
    if (!open || campaignName.trim() || !selectedStoreLabel) return;
    const stamp = new Date().toISOString().slice(0, 10).replaceAll('-', '');
    setCampaignName(`${selectedStoreLabel} 智能 GMV Max ${stamp}`);
  }, [campaignName, open, selectedStoreLabel]);

  const identityParams = useMemo(
    () => ({
      store_id: selectedStoreId || undefined,
      advertiser_id: advertiserId || undefined,
      store_authorized_bc_id: storeAuthorizedBcId,
    }),
    [advertiserId, selectedStoreId, storeAuthorizedBcId],
  );

  const identitiesQuery = useGmvMaxIdentitiesQuery(workspaceId, provider, authId, identityParams, {
    enabled: Boolean(open && workspaceId && provider && authId && selectedStoreId),
  });

  const identityOptions = useMemo(() => {
    const payload = identitiesQuery.data || {};
    const list = ensureArray(payload.identities || payload.identity_list || payload.items);
    const directOptions = list.map(normalizeIdentityOption).filter(Boolean);
    if (directOptions.length > 0) return directOptions;
    const precheckList = ensureArray(precheckResult?.available_identities);
    return precheckList.map(normalizeIdentityOption).filter(Boolean);
  }, [identitiesQuery.data, precheckResult]);

  const identityOptionMap = useMemo(() => {
    const map = new Map();
    identityOptions.forEach((item) => map.set(item.value, item));
    return map;
  }, [identityOptions]);

  useEffect(() => {
    if (!open || identityOptions.length === 0) return;
    setSelectedIdentities((prev) => {
      const existing = Array.from(prev).filter((id) => identityOptionMap.has(id));
      if (existing.length > 0) return new Set(existing.slice(0, IDENTITY_LIMIT));
      return new Set(identityOptions.slice(0, IDENTITY_LIMIT).map((item) => item.value));
    });
  }, [identityOptionMap, identityOptions, open]);

  const identityBusinessCenterId = useMemo(() => {
    const firstIdentity = Array.from(selectedIdentities)[0];
    if (!firstIdentity) return undefined;
    const data = identityOptionMap.get(firstIdentity)?.data || {};
    return (
      data.identity_authorized_bc_id ||
      data.identityAuthorizedBcId ||
      data.authorized_bc_id ||
      undefined
    );
  }, [identityOptionMap, selectedIdentities]);

  const bcIdForRequest = identityBusinessCenterId || storeBusinessCenterId;

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    {
      store_id: selectedStoreId || undefined,
      advertiser_id: advertiserId || undefined,
      owner_bc_id: bcIdForRequest || undefined,
      ad_creation_eligible: 'GMV_MAX',
      status: 'AVAILABLE',
      page_size: 500,
    },
    {
      enabled: Boolean(open && workspaceId && provider && authId && selectedStoreId),
    },
  );

  const productsData = useMemo(() => {
    const payload = productsQuery.data;
    const list = payload?.items || payload?.list || payload || [];
    return Array.isArray(list) ? list : [];
  }, [productsQuery.data]);

  const selectedProducts = useMemo(() => {
    const selected = new Set(Array.from(selectedItemIds).map(String));
    if (selected.size === 0) return productsData;
    return productsData.filter((product) => selected.has(String(getProductIdentifier(product))));
  }, [productsData, selectedItemIds]);

  const recommendedEffectivePrice = useMemo(() => {
    const prices = selectedProducts
      .map(getProductPrice)
      .map((value) => Number(value))
      .filter((value) => Number.isFinite(value) && value > 0);
    if (prices.length === 0) return undefined;
    return Math.min(...prices);
  }, [selectedProducts]);

  const precheckMutation = useGmvMaxPrecheckMutation(workspaceId, provider, authId);

  const currentPrecheckParams = useMemo(() => {
    if (!selectedStoreId) return null;
    const itemGroupIds =
      productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE ? sortIds(selectedItemIds) : [];
    return {
      store_id: selectedStoreId,
      store_authorized_bc_id: storeAuthorizedBcId,
      bc_id: bcIdForRequest || undefined,
      advertiser_id: advertiserId || undefined,
      identity_id: null,
      product_specific_type: productSpecificType,
      item_group_ids: itemGroupIds,
    };
  }, [
    advertiserId,
    bcIdForRequest,
    productSpecificType,
    selectedItemIds,
    selectedStoreId,
    storeAuthorizedBcId,
  ]);

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
      setPrecheckError('请选择店铺后再进行预检');
      return null;
    }
    if (
      currentPrecheckParams.product_specific_type === CUSTOM_PRODUCT_SPECIFIC_TYPE &&
      currentPrecheckParams.item_group_ids.length === 0
    ) {
      setPrecheckError('请先选择至少一个商品');
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
    if (!open || !currentPrecheckParams) return;
    if (hasPendingSeriesCreatePayload) return;
    if (
      currentPrecheckParams.product_specific_type === CUSTOM_PRODUCT_SPECIFIC_TYPE &&
      currentPrecheckParams.item_group_ids.length === 0
    ) {
      return;
    }
    const key = JSON.stringify(currentPrecheckParams);
    if (autoPrecheckKeyRef.current === key) return;
    autoPrecheckKeyRef.current = key;
    performPrecheck();
  }, [
    currentPrecheckParams,
    hasPendingSeriesCreatePayload,
    open,
    performPrecheck,
  ]);

  useEffect(() => {
    if (!open || productSpecificType !== DEFAULT_PRODUCT_SPECIFIC_TYPE) return;
    if (precheckResult?.promote_all_products_allowed !== false) return;
    setProductSpecificType(CUSTOM_PRODUCT_SPECIFIC_TYPE);
    if (autoSelectedProductsRef.current) return;
    const unoccupiedIds = sortIds(precheckResult.unoccupied_item_group_ids || precheckResult.available_item_group_ids || []);
    const productIds = productsData.map(getProductIdentifier).filter(Boolean);
    const nextIds = unoccupiedIds.length > 0 ? unoccupiedIds : productIds;
    if (nextIds.length > 0) {
      setSelectedItemIds(new Set(nextIds));
      autoSelectedProductsRef.current = true;
    }
  }, [open, precheckResult, productSpecificType, productsData]);

  useEffect(() => {
    if (!open || productSpecificType !== CUSTOM_PRODUCT_SPECIFIC_TYPE || selectedItemIds.size > 0) return;
    if (precheckResult?.promote_all_products_allowed !== false) return;
    const productIds = productsData.map(getProductIdentifier).filter(Boolean);
    if (productIds.length > 0) {
      setSelectedItemIds(new Set(productIds));
      autoSelectedProductsRef.current = true;
    }
  }, [open, precheckResult, productSpecificType, productsData, selectedItemIds.size]);

  const recommendedRoas = getRecommendedValue(
    precheckResult?.recommended_roas_bid,
    precheckResult?.recommended_roas,
  );
  const recommendedBudget = getRecommendedValue(
    precheckResult?.recommended_budget,
    precheckResult?.min_budget,
  );
  const currency = selectedStore?.currency || selectedStore?.currency_code || selectedStore?.currencyCode;

  useEffect(() => {
    if (!open || !precheckMatches) return;
    if (!budget && recommendedBudget !== undefined) setBudget(String(recommendedBudget));
    if (!roasBid && recommendedRoas !== undefined) setRoasBid(String(recommendedRoas));
    if (!effectivePrice && recommendedEffectivePrice !== undefined) {
      setEffectivePrice(String(recommendedEffectivePrice));
    }
  }, [
    budget,
    effectivePrice,
    open,
    precheckMatches,
    recommendedBudget,
    recommendedEffectivePrice,
    recommendedRoas,
    roasBid,
  ]);

  const toggleProduct = useCallback((id) => {
    setSelectedItemIds((prev) => {
      const next = new Set(prev);
      const key = String(id);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const toggleAll = useCallback((ids) => {
    setSelectedItemIds((prev) => {
      const next = new Set(prev);
      const normalized = (ids || []).map(String);
      const shouldDeselect = normalized.every((id) => next.has(id));
      if (shouldDeselect) normalized.forEach((id) => next.delete(id));
      else normalized.forEach((id) => next.add(id));
      return next;
    });
  }, []);

  const selectAllProducts = useCallback((ids) => {
    setSelectedItemIds(new Set((ids || []).map(String)));
  }, []);

  const clearAllProducts = useCallback(() => {
    setSelectedItemIds(new Set());
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

  const selectAllIdentities = useCallback(() => {
    setSelectedIdentities(new Set(identityOptions.slice(0, IDENTITY_LIMIT).map((item) => item.value)));
  }, [identityOptions]);

  const createMutation = useCreateGmvMaxCampaignMutation(workspaceId, provider, authId);

  const validationErrors = useMemo(() => {
    const nextErrors = {};
    if (!selectedStoreId) nextErrors.store_id = '请选择店铺';
    if (!campaignName.trim()) nextErrors.campaign_name = '系列名称不能为空';
    if (selectedIdentities.size > IDENTITY_LIMIT) nextErrors.identities = `最多选择 ${IDENTITY_LIMIT} 个身份`;
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
    const effectivePriceValue = parseOptionalFloat(effectivePrice);
    if (effectivePrice && (effectivePriceValue === undefined || Number.isNaN(effectivePriceValue) || effectivePriceValue <= 0)) {
      nextErrors.effectivePrice = '请输入大于 0 的有效成交价';
    }
    if (scheduleType === SCHEDULE_START_END) {
      if (!scheduleStart) nextErrors.scheduleStart = '请选择开始时间';
      if (!scheduleEnd) nextErrors.scheduleEnd = '请选择结束时间';
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
    effectivePrice,
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
      if (target.is_gmv_max_available === false) return '当前店铺不支持 Product GMV Max';
      if (target.needs_exclusive_auth) return '当前授权账号与店铺独占授权不匹配，请切换正确的授权账号';
      if (
        productSpecificType === DEFAULT_PRODUCT_SPECIFIC_TYPE &&
        target.promote_all_products_allowed === false
      ) {
        return '该店铺已有启用中的智能 GMV Max 系列，系统已切换为指定商品创建';
      }
      return '';
    },
    [precheckResult, productSpecificType],
  );

  const hasBlockingPrecheckIssue = useMemo(() => describeBlockingPrecheckIssue(), [
    describeBlockingPrecheckIssue,
  ]);

  const primaryErrorMessage = useMemo(() => {
    const keyOrder = ['store_id', 'campaign_name', 'identities', 'products', 'budget', 'roasBid', 'scheduleStart', 'scheduleEnd'];
    const keys = Object.keys(errors || {});
    if (keys.length === 0) return '';
    const prioritizedKey = keyOrder.find((key) => keys.includes(key));
    if (prioritizedKey && errors[prioritizedKey]) return errors[prioritizedKey];
    return errors[keys[0]] || '请先修正表单错误';
  }, [errors]);

  const disableCreateReason = useMemo(() => {
    if (hasPendingSeriesCreatePayload) return '';
    if (Object.keys(errors).length > 0) return primaryErrorMessage || '请先修正表单错误';
    if (!precheckMatches) return '请先完成预检';
    if (hasBlockingPrecheckIssue && productSpecificType === DEFAULT_PRODUCT_SPECIFIC_TYPE) return hasBlockingPrecheckIssue;
    return '';
  }, [
    errors,
    hasBlockingPrecheckIssue,
    hasPendingSeriesCreatePayload,
    precheckMatches,
    primaryErrorMessage,
    productSpecificType,
  ]);

  const handleUseRecommended = useCallback(() => {
    if (!precheckMatches) return;
    if (recommendedRoas !== undefined) setRoasBid(String(recommendedRoas));
    if (recommendedBudget !== undefined) setBudget(String(recommendedBudget));
    if (recommendedEffectivePrice !== undefined) setEffectivePrice(String(recommendedEffectivePrice));
  }, [precheckMatches, recommendedBudget, recommendedEffectivePrice, recommendedRoas]);

  const buildIdentityList = useCallback(() => {
    return Array.from(selectedIdentities).map((id) => {
      const data = identityOptionMap.get(id)?.data || {};
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
    const createIntentStorageKey = seriesCreateIntentStorageKey;
    const storedCreateIntent = createIntentStorageKey
      ? getStoredCampaignCreateIntent({
          storageKey: createIntentStorageKey,
          fallbackIntents: campaignCreateIntentFallbackRef.current,
        })
      : null;
    let payload = getFinalizedCampaignCreatePayload(storedCreateIntent);

    if (payload) {
      try {
        const createResult = await createMutation.mutateAsync(payload);
        if (['QUARANTINED', 'QUARANTINE_PENDING'].includes(createResult?.creation_status)) {
          const warning = ensureArray(createResult?.warnings)[0];
          setSubmitError(
            warning?.message ||
              '系列已经创建，但智能策略初始化或安全暂停尚未完成。请再次点击创建以恢复同一次请求，不要重复创建。',
          );
          return;
        }
        clearCampaignCreateIntent(
          createIntentStorageKey,
          campaignCreateIntentFallbackRef.current,
        );
        onCreated?.();
      } catch (error) {
        if (isDefinitiveCreateRejection(error)) {
          clearCampaignCreateIntent(
            createIntentStorageKey,
            campaignCreateIntentFallbackRef.current,
          );
        }
        setSubmitError(formatError(error));
      }
      return;
    }

    const latestErrors = Object.keys(validationErrors).length > 0 ? validationErrors : errors;
    if (Object.keys(latestErrors).length > 0) {
      setErrors(latestErrors);
      return;
    }
    let latestPrecheckResult = precheckResult;
    try {
      if (!precheckMatches) latestPrecheckResult = await performPrecheck();
    } catch {
      return;
    }

    const blockingIssue = describeBlockingPrecheckIssue(latestPrecheckResult);
    if (!latestPrecheckResult || (blockingIssue && productSpecificType === DEFAULT_PRODUCT_SPECIFIC_TYPE)) {
      setSubmitError(blockingIssue || '预检未通过');
      return;
    }

    const selectedProductIds = productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE ? sortIds(selectedItemIds) : [];
    const effectivePriceValue = parseOptionalFloat(effectivePrice);
    const productEffectivePrices =
      effectivePriceValue && selectedProductIds.length > 0
        ? Object.fromEntries(selectedProductIds.map((id) => [id, effectivePriceValue]))
        : {};
    const createIntent = getOrCreateCampaignCreateIntent({
      storageKey: createIntentStorageKey,
      campaignName: campaignName.trim(),
      fallbackIntents: campaignCreateIntentFallbackRef.current,
    });
    payload = finalizeCampaignCreateIntent({
      storageKey: createIntentStorageKey,
      intent: createIntent,
      fallbackIntents: campaignCreateIntentFallbackRef.current,
      createPayload: {
        request_id: createIntent.request_id,
        idempotency_key: createIntent.request_id,
        store_id: selectedStoreId,
        campaign_name: createIntent.campaign_name,
        product_specific_type: productSpecificType,
        item_group_ids:
          productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE ? selectedProductIds : undefined,
        roas_bid: parseOptionalFloat(roasBid),
        budget: parseOptionalFloat(budget),
        schedule_type: scheduleType,
        schedule_start_time: toIsoString(scheduleStart),
        schedule_end_time: toIsoString(scheduleEnd) || null,
        product_video_specific_type: 'AUTO_SELECTION',
        identity_list: buildIdentityList(),
        advertiser_id: advertiserId ? String(advertiserId) : undefined,
        automation: {
          enabled: true,
          smart_guard_enabled: true,
          creative_guard_enabled: true,
          auto_heating_enabled: true,
          hermes_enabled: true,
          cooldown_minutes: 30,
          monitor_interval_minutes: 3,
          evaluation_window_minutes: 60,
          min_runtime_minutes_before_first_change: 10,
          min_spend_cents: 300,
          min_roi: 0.8,
          product_effective_prices: productEffectivePrices,
          default_effective_product_price:
            effectivePriceValue && selectedProductIds.length === 0 ? effectivePriceValue : undefined,
        },
      },
    });

    try {
      const createResult = await createMutation.mutateAsync(payload);
      if (['QUARANTINED', 'QUARANTINE_PENDING'].includes(createResult?.creation_status)) {
        const warning = ensureArray(createResult?.warnings)[0];
        setSubmitError(
          warning?.message ||
            '系列已经创建，但智能策略初始化或安全暂停尚未完成。请再次点击创建以恢复同一次请求，不要重复创建。',
        );
        return;
      }
      clearCampaignCreateIntent(
        createIntentStorageKey,
        campaignCreateIntentFallbackRef.current,
      );
      onCreated?.();
    } catch (error) {
      if (isDefinitiveCreateRejection(error)) {
        clearCampaignCreateIntent(
          createIntentStorageKey,
          campaignCreateIntentFallbackRef.current,
        );
      }
      setSubmitError(formatError(error));
    }
  }, [
    advertiserId,
    budget,
    buildIdentityList,
    campaignName,
    createMutation,
    describeBlockingPrecheckIssue,
    effectivePrice,
    errors,
    onCreated,
    performPrecheck,
    precheckMatches,
    precheckResult,
    productSpecificType,
    roasBid,
    scheduleEnd,
    scheduleStart,
    scheduleType,
    selectedItemIds,
    selectedStoreId,
    validationErrors,
    seriesCreateIntentStorageKey,
  ]);

  if (!open) return null;

  const occupancyInfo = precheckResult
    ? {
        available: ensureArray(precheckResult.unoccupied_item_group_ids || precheckResult.available_item_group_ids).length,
        occupied: ensureArray(precheckResult.occupied_item_group_ids).length,
      }
    : null;

  return (
    <Modal open={open} title={'选择商品智能投放'} onClose={onClose}>
      {optionsQuery.isLoading ? <Loading text="选项加载中..." /> : null}
      <ErrorBlock error={optionsQuery.error} onRetry={optionsQuery.refetch} />

      {hasBlockingPrecheckIssue ? (
        <div className="gmvmax-banner gmvmax-banner--warning">{hasBlockingPrecheckIssue}</div>
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
            placeholder="请输入系列名称"
          />
        </FormField>

        <div className="gmvmax-inline-tags">
          <span className="gmvmax-tag">广告投放类型：PRODUCT</span>
          <span className="gmvmax-tag">优化目标：VALUE (GMV)</span>
          <span className="gmvmax-tag">出价类型：VO_MIN_ROAS</span>
        </div>
      </section>

      <section className="gmvmax-section">
        <h3>选择商品与授权身份</h3>
        <FormField label="投放范围">
          <div className="gmvmax-radio-group">
            <label className="gmvmax-radio-card">
              <input
                type="radio"
                name="product_specific_type"
                value={DEFAULT_PRODUCT_SPECIFIC_TYPE}
                checked={productSpecificType === DEFAULT_PRODUCT_SPECIFIC_TYPE}
                onChange={() => setProductSpecificType(DEFAULT_PRODUCT_SPECIFIC_TYPE)}
              />
              <span>
                <strong>推广店铺所有商品</strong>
                <small>仅当店铺没有启用中的全店智能 GMV Max 时可用</small>
              </span>
            </label>
            <label className="gmvmax-radio-card">
              <input
                type="radio"
                name="product_specific_type"
                value={CUSTOM_PRODUCT_SPECIFIC_TYPE}
                checked={productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE}
                onChange={() => setProductSpecificType(CUSTOM_PRODUCT_SPECIFIC_TYPE)}
              />
              <span>
                <strong>只推广选定商品</strong>
                <small>适合已有全店系列时继续补充单品测试</small>
              </span>
            </label>
          </div>
        </FormField>

        {productSpecificType === CUSTOM_PRODUCT_SPECIFIC_TYPE ? (
          <div className="gmvmax-card gmvmax-card--flat">
            {occupancyInfo ? (
              <div className="gmvmax-tip">
                当前店铺 GMV Max 商品占用：{occupancyInfo.available} 个可用 / {occupancyInfo.occupied} 个已占用
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
              emptyMessage={productsQuery.isLoading ? '商品加载中...' : '该店铺暂无可投放商品。'}
              searchTerm={productSearch}
              onSearchChange={setProductSearch}
            />
            {errors.products ? <div className="gmvmax-error-text">{errors.products}</div> : null}
          </div>
        ) : null}

        <FormField label={`身份（最多 ${IDENTITY_LIMIT} 个）`} error={errors.identities}>
          {identitiesQuery.isLoading ? <Loading text="身份加载中..." /> : null}
          {identityOptions.length === 0 ? (
            <p className="gmvmax-placeholder">暂无可用身份</p>
          ) : (
            <>
              <div className="gmvmax-check-toolbar">
                <button
                  type="button"
                  className="gmvmax-button gmvmax-button--secondary"
                  onClick={selectAllIdentities}
                  disabled={identityOptions.length === 0}
                >
                  全选已授权身份
                </button>
                <span className="gmvmax-tip">已选 {selectedIdentities.size} / {identityOptions.length}</span>
              </div>
              <div className="gmvmax-check-list">
                {identityOptions.map((identity) => (
                  <label key={identity.value} className="gmvmax-check-item">
                    <input
                      type="checkbox"
                      checked={selectedIdentities.has(identity.value)}
                      onChange={() => toggleIdentity(identity.value)}
                      disabled={!selectedIdentities.has(identity.value) && selectedIdentities.size >= IDENTITY_LIMIT}
                    />
                    <span>{identity.label || identity.value}</span>
                  </label>
                ))}
              </div>
            </>
          )}
        </FormField>

        <FormField label="创意模式">
          <div className="gmvmax-radio-group">
            <label className="gmvmax-radio-card">
              <input type="radio" checked readOnly />
              <span>
                <strong>系统自动选择视频</strong>
                <small>优先保证素材供应，后续由创意监控剔除低效素材</small>
              </span>
            </label>
          </div>
        </FormField>
      </section>

      <section className="gmvmax-section">
        <h3>系统推荐出价与排期</h3>
        <div className="gmvmax-modal-grid">
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
          </FormField>
          <FormField label="有效成交价" error={errors.effectivePrice}>
            <div className="gmvmax-inline-input">
              <input
                type="number"
                min="0"
                value={effectivePrice}
                onChange={(event) => setEffectivePrice(event.target.value)}
                placeholder="例如 10"
              />
              <span className="gmvmax-plain-text">{currency || 'USD'}</span>
            </div>
            <div className="gmvmax-tip">用于自动暂停、素材排除和商品卡重建；为空时系统使用历史成交均价，最后才使用商品原价。</div>
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
              <button
                type="button"
                className="gmvmax-button gmvmax-button--secondary"
                onClick={handleUseRecommended}
                disabled={!precheckMatches || recommendedRoas === undefined}
              >
                使用推荐值
              </button>
            </div>
          </FormField>
        </div>
        {precheckMatches && (recommendedRoas !== undefined || recommendedBudget !== undefined) ? (
          <div className="gmvmax-tip">
            推荐出价：{recommendedRoas ?? '-'}，推荐预算：
            {recommendedBudget !== undefined ? formatMoney(recommendedBudget) : '-'}
          </div>
        ) : (
          <div className="gmvmax-tip">预检通过后会自动读取推荐预算和推荐 ROAS。</div>
        )}

        <FormField label="排期类型">
          <div className="gmvmax-radio-group">
            <label className="gmvmax-radio-card">
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
              <span>
                <strong>立即开始，长期运行</strong>
                <small>创建后立即投放</small>
              </span>
            </label>
            <label className="gmvmax-radio-card">
              <input
                type="radio"
                name="schedule_type"
                value={SCHEDULE_START_END}
                checked={scheduleType === SCHEDULE_START_END}
                onChange={() => setScheduleType(SCHEDULE_START_END)}
              />
              <span>
                <strong>指定开始和结束时间</strong>
                <small>适合固定测试窗口</small>
              </span>
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
        <button
          type="button"
          className="gmvmax-button gmvmax-button--secondary"
          onClick={performPrecheck}
          disabled={!selectedStoreId || precheckMutation.isPending}
        >
          {precheckMutation.isPending ? '预检中...' : '重新预检'}
        </button>
        {hasPendingSeriesCreatePayload ? (
          <span className="gmvmax-tip">检测到未完成的新建请求，将继续同一次请求，不会重复创建。</span>
        ) : !precheckMatches ? (
          <span className="gmvmax-tip">预检后才能创建系列</span>
        ) : null}
      </div>

      {submitError ? <div className="gmvmax-error">{submitError}</div> : null}
      {createMutation.isPending ? <Loading text="正在创建系列..." /> : null}

      <div className="gmvmax-modal-footer">
        <button
          type="button"
          className="gmvmax-button gmvmax-button--ghost"
          onClick={onClose}
          disabled={createMutation.isPending}
        >
          {GmvMaxTexts.cancel || '取消'}
        </button>
        <button
          type="button"
          className="gmvmax-button gmvmax-button--primary"
          onClick={handleSubmit}
          disabled={createMutation.isPending || Boolean(disableCreateReason)}
          title={disableCreateReason || ''}
        >
          {hasPendingSeriesCreatePayload
            ? '继续完成智能投放'
            : GmvMaxTexts.createSeries || '新建智能投放'}
        </button>
      </div>
    </Modal>
  );
}
