import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import Loading from '@/components/ui/Loading.jsx';
import { listTikTokAccounts } from '../../../integrations/tiktok_business/service.js';

import {
  applyGmvMaxAction,
  createGmvMaxCampaign,
  listGmvMaxCreativeAssets,
  precheckGmvMaxCampaign,
  refreshGmvMaxCreativeAssets,
  updateGmvMaxStrategy,
  uploadGmvMaxCreativeAsset,
} from '../../api/gmvMaxApi.js';
import {
  ensureArray,
  extractProductsFromDetail,
  formatError,
  formatMoney,
  getProductIdentifier,
  isCampaignEnabledStatus,
  isProductAvailable,
  parseOptionalFloat,
} from './helpers.js';
import {
  buildCampaignCreateIntentKey,
  clearCampaignCreateIntent,
  finalizeCampaignCreateIntent,
  getFinalizedCampaignCreatePayload,
  getOrCreateCampaignCreateIntent,
  getStoredCampaignCreateIntent,
  isDefinitiveCreateRejection,
} from '../../utils/campaignCreateIntent.js';

const IDENTITY_LIMIT = 20;
const PRODUCT_SPECIFIC_TYPE = 'CUSTOMIZED_PRODUCTS';
const CREATIVE_MODE_AUTO = 'AUTO_SELECTION';
const CREATIVE_MODE_MANUAL = 'CUSTOM_SELECTION';
const LAUNCH_MODE_SMART = 'SMART';
const LAUNCH_MODE_MANUAL = 'MANUAL';
const ACTION_RESTORE = 'RESTORE';
const ACTION_CREATE = 'CREATE';
const STATS_RANGE_OPTIONS = [
  { key: 'today', label: '今日' },
  { key: 'yesterday', label: '昨天' },
  { key: '7d', label: '近7天' },
  { key: '30d', label: '近30天' },
  { key: 'custom', label: '自定义' },
];

function getRecommendedValue(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return undefined;
}

function getProductName(product, fallback) {
  return (
    product?.title ||
    product?.name ||
    product?.product_name ||
    product?.productName ||
    product?.item_name ||
    product?.itemName ||
    fallback
  );
}

function getProductImage(product) {
  return (
    product?.image_url ||
    product?.product_image_url ||
    product?.cover_image ||
    product?.thumbnail_url ||
    product?.imageUrl ||
    product?.coverImage ||
    product?.main_image ||
    null
  );
}

function getProductPrice(product) {
  return getRecommendedValue(
    product?.effective_price,
    product?.effectivePrice,
    product?.sale_price,
    product?.salePrice,
    product?.min_price,
    product?.minPrice,
    product?.price,
  );
}

function getProductPriceSourceLabel(product) {
  const source = String(
    product?.effective_price_source || product?.effectivePriceSource || '',
  ).toLowerCase();
  if (source === 'tiktok_shop_flash_sale') return 'TikTok Shop 闪购活动价';
  if (source === 'tiktok_shop_latest_transaction') return 'TikTok Shop 最新成交价';
  if (source === 'tiktok_shop_listing') return 'TikTok Shop 当前售价';
  return source ? 'TikTok Shop 官方价格' : '等待 TikTok Shop 价格同步';
}

function shortId(value) {
  const text = String(value || '');
  if (text.length <= 14) return text;
  return `${text.slice(0, 7)}...${text.slice(-5)}`;
}


function formatMetricNumber(value, digits = 0) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return number.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function getAutomationStats(product) {
  return product?.gmvmax_automation_stats || product?.gmvmaxAutomationStats || null;
}

function buildAutomationMetricRows(stats) {
  // Campaign performance is a campaign-wide total and must never be used as
  // a product fallback.  The card only renders the canonical
  // latest-campaign × product facts returned with the product.
  if (!stats) return [];
  const spend = stats.spend ?? 0;
  const gmv = stats.gmv ?? stats.gross_revenue ?? 0;
  const orders = stats.orders ?? 0;
  const roas = Number(stats.roas ?? (Number(spend) > 0 ? Number(gmv) / Number(spend) : 0));
  return [
    [
      { label: '花费', value: formatMoney(spend) },
      { label: '成交 GMV', value: formatMoney(gmv) },
      { label: '订单', value: formatMetricNumber(orders) },
      { label: 'ROAS', value: Number.isFinite(roas) ? roas.toFixed(2) : '—' },
    ],
    [
      { label: '计划', value: formatMetricNumber(stats?.campaign_count || 0) },
      { label: '暂停', value: formatMetricNumber(stats?.pause_count || 0) },
      { label: '重建', value: formatMetricNumber(stats?.reset_count || 0) },
      { label: '排除', value: formatMetricNumber(stats?.creative_exclude_count || 0) },
    ],
  ];
}

function truncateName(value, maxLength = 72) {
  const text = String(value || '');
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 1)).trim()}...`;
}

function formatAutomationReason(reason) {
  const text = String(reason || '').trim();
  if (!text) return '';
  if (text.includes('daily spend cap reached')) return '已达到每日消耗上限，等待下一广告日恢复';
  if (text.includes('campaign exceeded budget before guard window caught spend')) {
    return '快速消耗超过监控窗口，已执行事故止损暂停';
  }
  if (text.includes('cooldown active')) return '止损冷却期内，系统将在复核时间重新评估，不保证自动恢复';
  if (text.includes('cooldown complete')) return '冷却条件满足并经审批，系统已恢复投放';
  if (text.includes('data conflict protective pause')) return '跨层数据存在冲突，系统已短暂停并强制同步核验';
  if (text.includes('data conflict awaiting attribution')) return '订单归因尚未一致，系统等待同步和 Hermes 复核';
  if (text.includes('Hermes deferred recovery')) return 'Hermes 认为当前证据不足，暂缓恢复并安排再次复核';
  if (text.includes('inherit_historical_exclusions')) return '新计划已继承历史低效素材排除记录';
  if (text.includes('no_spend_timeout')) return '长时间无消耗，系统已重建计划';
  return text;
}

function formatCreativeUploadError(message) {
  const text = String(message || '').trim();
  if (!text) return '视频上传到 TikTok 失败，文件已保留，可稍后重试。';
  if (text.includes('Failed to fetch url data')) {
    return 'TikTok 回源读取视频失败，文件已保留，请稍后重试。';
  }
  return text.replace(/^TikTok upload failed:\s*/i, 'TikTok 上传失败：');
}

function parseAutomationDate(value) {
  if (!value) return null;
  const text = String(value).trim();
  if (!text) return null;
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function dateKeyInTimezone(value, timeZone) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: timeZone || undefined,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(value);
}

function formatAutomationStartTime(value, timeZone) {
  const date = parseAutomationDate(value);
  if (!date) return '';
  if (date.getTime() <= Date.now()) return '';
  const now = new Date();
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  const time = new Intl.DateTimeFormat('zh-CN', {
    timeZone: timeZone || undefined,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
  if (dateKeyInTimezone(date, timeZone) === dateKeyInTimezone(now, timeZone)) return `\u4eca\u65e5 ${time}`;
  if (dateKeyInTimezone(date, timeZone) === dateKeyInTimezone(tomorrow, timeZone)) return `\u660e\u65e5 ${time}`;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: timeZone || undefined,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

function getCreativeTitle(asset) {
  return asset?.title || asset?.creative_name || asset?.creativeName || asset?.item_id || '未命名视频';
}

function getCreativeCover(asset) {
  const candidates = [
    asset?.local_cover_url,
    asset?.video_cover_url,
    asset?.thumbnail_url,
    asset?.cover_url,
    asset?.coverUrl,
  ];
  return candidates.find((value) => String(value || '').startsWith('/api/v1/')) || null;
}

function getCreativeId(asset) {
  return String(asset?.item_id || asset?.creative_id || asset?.shop_content_id || '').trim();
}

function getCreativeMetrics(asset) {
  return asset?.metrics || {};
}

function getHermesTierLabel(asset) {
  const tier = String(asset?.hermes_tier || '').toUpperCase();
  if (tier === 'WINNER') return '优胜素材';
  if (tier === 'PROMISING') return '潜力素材';
  if (tier === 'EXPLORATION') return '探索候选';
  if (tier === 'REJECTED') return '历史已排除';
  if (tier === 'WEAK') return '表现偏弱';
  return '待积累数据';
}

function buildManualCreativePayload(assets, productId, fallbackIdentity, storeId) {
  const itemList = [];
  const customAnchorVideoList = [];
  assets.forEach((asset) => {
    if (!asset?.selectable) return;
    const itemId = getCreativeId(asset);
    const videoId = asset?.video_id || asset?.videoId;
    const identity = asset?.identity_info || asset?.identityInfo || fallbackIdentity;
    const identityId = identity?.identity_id || identity?.identityId;
    const identityType = identity?.identity_type || identity?.identityType;
    if (!itemId || !videoId || !identityId || !identityType) return;
    const identityInfo = {
      identity_id: String(identityId),
      identity_type: String(identityType),
      identity_authorized_bc_id: identity.identity_authorized_bc_id || identity.identityAuthorizedBcId || undefined,
      identity_authorized_shop_id: identity.identity_authorized_shop_id || identity.identityAuthorizedShopId || undefined,
      store_id: storeId,
    };
    const entry = {
      identity_info: identityInfo,
      item_id: String(itemId),
      spu_id_list: [String(productId)],
      video_info: { video_id: String(videoId) },
    };
    itemList.push(entry);
    customAnchorVideoList.push(entry);
  });
  return { itemList, customAnchorVideoList };
}

function toIsoString(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return undefined;
  return date.toISOString();
}

function normalizeIdentityOption(identity) {
  if (!identity || typeof identity !== 'object') return null;
  const value = String(identity.identity_id || identity.id || identity.identityId || '').trim();
  if (!value) return null;
  return { value, data: identity };
}

function buildIdentityList(identities, storeId) {
  return identities.slice(0, IDENTITY_LIMIT).map((identity) => {
    const data = identity.data || {};
    return {
      identity_id: identity.value,
      identity_type: data.identity_type || data.identityType || null,
      identity_authorized_bc_id:
        data.identity_authorized_bc_id || data.identityAuthorizedBcId || data.authorized_bc_id || null,
      identity_authorized_shop_id:
        data.identity_authorized_shop_id || data.identityAuthorizedShopId || null,
      store_id: storeId,
    };
  });
}

function campaignIdOf(card) {
  return card?.campaign?.campaign_id || card?.campaign?.id || card?.detail?.campaign?.campaign_id || card?.detail?.campaign?.id;
}

function campaignNameOf(card) {
  return card?.campaign?.campaign_name || card?.campaign?.name || card?.detail?.campaign?.campaign_name || card?.detail?.campaign?.name || '';
}

export function resolveProductCampaignOperationStatus(card, automationStats) {
  const canonicalCampaignId =
    automationStats?.latest_campaign_id || automationStats?.latestCampaignId || null;
  const cardCampaignId = campaignIdOf(card);
  const canonicalStatus =
    automationStats?.campaign_operation_status || automationStats?.campaignOperationStatus || null;
  if (
    canonicalStatus &&
    (!cardCampaignId || !canonicalCampaignId || String(cardCampaignId) === String(canonicalCampaignId))
  ) {
    return canonicalStatus;
  }
  return (
    card?.campaign?.operation_status ||
    card?.detail?.campaign?.operation_status ||
    canonicalStatus ||
    card?.campaign?.status ||
    card?.detail?.campaign?.status
  );
}

function campaignBudgetAmount(card) {
  const value = getRecommendedValue(
    card?.campaign?.budget,
    card?.campaign?.budget_value,
    card?.campaign?.budgetValue,
    card?.campaign?.budget_cents,
    card?.campaign?.budgetCents,
    card?.detail?.campaign?.budget,
    card?.detail?.campaign?.budget_value,
    card?.detail?.campaign?.budgetValue,
    card?.detail?.campaign?.budget_cents,
    card?.detail?.campaign?.budgetCents,
  );
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return undefined;
  return numeric > 1000 ? numeric / 100 : numeric;
}

function campaignHasProduct(card, productId) {
  const target = String(productId || '');
  if (!target) return false;
  const productIds = new Set();
  extractProductsFromDetail(card?.detail).forEach((product) => {
    const id = getProductIdentifier(product);
    if (id) productIds.add(String(id));
  });

  const addIds = (value) => {
    ensureArray(value).forEach((id) => {
      if (id) productIds.add(String(id));
    });
  };
  const addProducts = (value) => {
    ensureArray(value).forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) productIds.add(String(id));
    });
  };
  const rawSources = [
    card?.campaign,
    card?.detail,
    card?.detail?.campaign,
    card?.campaign?.detail_raw_json,
    card?.campaign?.detailRawJson,
    card?.campaign?.raw_json,
    card?.campaign?.rawJson,
    card?.detail?.detail_raw_json,
    card?.detail?.detailRawJson,
    card?.detail?.raw_json,
    card?.detail?.rawJson,
    card?.detail?.campaign?.detail_raw_json,
    card?.detail?.campaign?.detailRawJson,
    card?.detail?.campaign?.raw_json,
    card?.detail?.campaign?.rawJson,
  ].filter(Boolean);

  rawSources.forEach((source) => {
    addIds(source.item_group_ids);
    addIds(source.itemGroupIds);
    addIds(source.item_group_id_list);
    addIds(source.itemGroupIdList);
    addProducts(source.products);
    addProducts(source.product_list);
    addProducts(source.item_list);
  });
  return productIds.has(target);
}

function getStrategyPayload(card) {
  const strategy = card?.strategy?.strategy || card?.strategy || {};
  const config = strategy.config_json || strategy.configJson || {};
  return { strategy, config };
}

function isTruthyFlag(value) {
  if (typeof value === 'string') {
    return ['1', 'true', 'yes', 'on', 'enabled'].includes(value.trim().toLowerCase());
  }
  return Boolean(value);
}

function isDeletedCampaign(card, operationStatus) {
  const normalized = String(operationStatus || '').trim().toUpperCase();
  return Boolean(
    card?.isDeleted ||
    card?.campaign?.is_deleted ||
    card?.campaign?.isDeleted ||
    normalized === 'DELETE' ||
    normalized === 'STATUS_DELETE' ||
    normalized === 'CAMPAIGN_STATUS_DELETE' ||
    normalized.includes('DELETED'),
  );
}

function getStrategyEnabled(card) {
  const { strategy } = getStrategyPayload(card);
  return isTruthyFlag(strategy.enabled);
}

function isAutoManagedCampaign(card) {
  const { strategy, config } = getStrategyPayload(card);
  const smartGuard = config.smart_guard || config.smartGuard || {};
  const creativeGuard = config.creative_guard || config.creativeGuard || {};
  const hasStrategy =
    strategy.id ||
    strategy.campaign_id ||
    strategy.campaignId ||
    Object.keys(config).length > 0;
  return Boolean(
    hasStrategy &&
      (isTruthyFlag(config.hermes_enabled || config.hermesEnabled) ||
      isTruthyFlag(strategy.auto_heating_enabled || strategy.autoHeatingEnabled) ||
      isTruthyFlag(smartGuard.enabled) ||
      isTruthyFlag(smartGuard.hermes_enabled || smartGuard.hermesEnabled) ||
      isTruthyFlag(creativeGuard.enabled)),
  );
}

function resolveProductCampaign(productId, campaignCards, preferredCampaignId) {
  const allCards = ensureArray(campaignCards);
  const preferred = preferredCampaignId
    ? allCards.find((card) => String(campaignIdOf(card) || '') === String(preferredCampaignId))
    : null;
  if (preferred) return preferred;
  // A preferred campaign id comes from the backend's canonical automation
  // state.  If the overview is filtered to running campaigns, the matching
  // paused card is intentionally absent.  Falling through to another
  // same-product ENABLE card would silently change the entity whose state and
  // actions are displayed.
  if (preferredCampaignId) return null;
  const candidates = allCards.filter((card) => campaignHasProduct(card, productId));
  const autoEnabled = candidates.find((card) => isAutoManagedCampaign(card) && isCampaignEnabledStatus(
    card?.campaign?.operation_status ||
      card?.campaign?.status ||
      card?.detail?.campaign?.operation_status ||
      card?.detail?.campaign?.status,
  ));
  const autoPaused = candidates.find((card) => isAutoManagedCampaign(card));
  const enabled = candidates.find((card) => isCampaignEnabledStatus(
    card?.campaign?.operation_status ||
      card?.campaign?.status ||
      card?.detail?.campaign?.operation_status ||
      card?.detail?.campaign?.status,
  ));
  return autoEnabled || autoPaused || enabled || candidates[0] || null;
}

function resolveStoreAuthorizedBcId(bindingConfig, businessCenterId) {
  return (
    bindingConfig?.store_authorized_bc_id ||
    bindingConfig?.storeAuthorizedBcId ||
    bindingConfig?.authorized_bc_id ||
    bindingConfig?.bc_id ||
    businessCenterId ||
    undefined
  );
}

function buildCampaignName(product, productId, launchMode = LAUNCH_MODE_SMART) {
  const date = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  const stamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}_${pad(date.getHours())}${pad(date.getMinutes())}`;
  const name = truncateName(getProductName(product, productId), 18).replace(/\s+/g, '');
  const modeLabel = launchMode === LAUNCH_MODE_MANUAL ? '手动投放' : '智能投放';
  return `${name || '商品'}_${modeLabel}_${stamp}`;
}

function buildStrategyPayload(referencePrice, dailySpendCap, productId) {
  const price = Number(referencePrice);
  const cap = Number(dailySpendCap);
  const productKey = String(productId || '').trim();
  const effectivePrices = productKey && Number.isFinite(price) && price > 0
    ? { [productKey]: price }
    : {};
  const noOrderSpendCents = Number.isFinite(price) && price > 0 ? Math.round(price * 100 * 2.5) : 300;
  return {
    enabled: true,
    auto_heating_enabled: true,
    cooldown_minutes: 30,
    min_runtime_minutes_before_first_change: 10,
    target_roi: 3,
    min_roi: 0.8,
    config_json: {
      hermes_enabled: true,
      smart_guard: {
        enabled: true,
        hermes_enabled: true,
        hermes_auto_apply: true,
        monitor_interval_minutes: 1,
        fast_monitor_interval_minutes: 1,
        normal_monitor_interval_minutes: 3,
        slow_monitor_interval_minutes: 5,
        evaluation_window_minutes: 60,
        pause_cooldown_minutes: 30,
        min_spend_cents: 300,
        min_roi: 0.8,
        daily_spend_cap_enabled: Number.isFinite(cap) && cap > 0,
        daily_spend_cap_cents: Number.isFinite(cap) && cap > 0 ? Math.round(cap * 100) : null,
        daily_budget_pacing: true,
        product_effective_prices: effectivePrices,
      },
      creative_guard: {
        enabled: true,
        use_effective_product_price: true,
        no_order_spend_cents: noOrderSpendCents,
        product_card_reset: { enabled: true, recreate: true, disable_old_strategy: true },
        no_order_budget_share_floor: '0.0',
        product_effective_prices: effectivePrices,
      },
    },
  };
}

export default function ProductAutomationPanel({
  workspaceId,
  provider,
  authId,
  advertiserId,
  businessCenterId,
  storeId,
  advertiserTimezone,
  bindingConfig,
  products,
  productsLoading,
  productsRefreshing = false,
  campaignCards,
  canOperate,
  onChanged,
  statsRangeKey = 'today',
  statsCustomRange = { start: '', end: '' },
  statsRangeLabel = '',
  onStatsRangeChange,
  onStatsCustomRangeChange,
}) {
  const [dailyCapByProduct, setDailyCapByProduct] = useState({});
  const [pendingProductId, setPendingProductId] = useState('');
  const [notice, setNotice] = useState(null);
  const [search, setSearch] = useState('');
  const [creativeModeByProduct, setCreativeModeByProduct] = useState({});
  const [selectedCreativeByProduct, setSelectedCreativeByProduct] = useState({});
  const [creativeCandidatesByProduct, setCreativeCandidatesByProduct] = useState({});
  const [creativeSearchByProduct, setCreativeSearchByProduct] = useState({});
  const [creativeRecommendationByProduct, setCreativeRecommendationByProduct] = useState({});
  const [creativeLoadingByProduct, setCreativeLoadingByProduct] = useState({});
  const [creativeErrorByProduct, setCreativeErrorByProduct] = useState({});
  const [uploadingProductId, setUploadingProductId] = useState('');
  const [uploadDialog, setUploadDialog] = useState(null);
  const [tiktokAccounts, setTiktokAccounts] = useState([]);
  const [tiktokAccountsLoading, setTiktokAccountsLoading] = useState(false);
  const [tiktokAccountsError, setTiktokAccountsError] = useState('');
  const [expandedProductOrder, setExpandedProductOrder] = useState([]);
  const [launchModeByProduct, setLaunchModeByProduct] = useState({});
  const [launchActionByProduct, setLaunchActionByProduct] = useState({});
  const [launchPickerProductId, setLaunchPickerProductId] = useState('');
  const campaignCreateIntentFallbackRef = useRef(new Map());

  const expandedStorageKey = useMemo(
    () => `gmvmax.product-cards.expanded.v1:${workspaceId || ''}:${storeId || ''}`,
    [storeId, workspaceId],
  );

  const productRows = useMemo(() => {
    const query = search.trim().toLowerCase();
    return ensureArray(products)
      .filter((product) => isProductAvailable(product))
      .filter((product) => {
        if (!query) return true;
        const id = getProductIdentifier(product);
        const name = getProductName(product, '');
        return String(id || '').toLowerCase().includes(query) || String(name || '').toLowerCase().includes(query);
      });
  }, [products, search]);

  const statusByProduct = useMemo(() => {
    const map = new Map();
    productRows.forEach((product) => {
      const id = getProductIdentifier(product);
      if (!id) return;
      const automationStats = getAutomationStats(product);
      const campaign = resolveProductCampaign(id, campaignCards, automationStats?.latest_campaign_id);
      const campaignId = automationStats?.latest_campaign_id || campaignIdOf(campaign);
      const operationStatus = resolveProductCampaignOperationStatus(campaign, automationStats);
      const canonicalActiveCount = Number(
        automationStats?.active_campaign_count ?? automationStats?.activeCampaignCount ?? 0,
      );
      const lifetimeCampaignCount = Number(
        automationStats?.lifetime_campaign_count ?? automationStats?.lifetimeCampaignCount ?? 0,
      );
      const occupied = String(product?.gmv_max_ads_status || product?.gmvMaxAdsStatus || '').toUpperCase() === 'OCCUPIED';
      const hasCampaign = lifetimeCampaignCount > 0 || Boolean(campaignId);
      const enabled = canonicalActiveCount > 0 || isCampaignEnabledStatus(operationStatus) || (
        occupied && !hasCampaign
      );
      const deleted = !enabled && (isDeletedCampaign(campaign, operationStatus) || Boolean(
        automationStats?.latest_campaign_deleted || automationStats?.latestCampaignDeleted,
      ));
      const automationStrategyEnabled = Boolean(
        automationStats?.strategy_enabled || automationStats?.strategyEnabled,
      );
      const autoManaged = Boolean(
        (campaign && isAutoManagedCampaign(campaign)) || automationStrategyEnabled,
      );
      const strategyEnabled = automationStats?.strategy_enabled ?? (campaign ? getStrategyEnabled(campaign) : false);
      const controlledTest = automationStats?.controlled_test || automationStats?.controlledTest || null;
      const controlledTestActive = Boolean(controlledTest?.active);
      const hasAdvertising = enabled;
      const label = autoManaged && strategyEnabled && hasAdvertising && controlledTestActive
        ? '受控测试中'
        : autoManaged && strategyEnabled && hasAdvertising
        ? '智能投放中'
        : autoManaged && strategyEnabled
          ? '智能暂停中'
          : autoManaged
            ? '智能已停止'
            : hasAdvertising
              ? '普通投放中'
              : deleted
                ? '历史系列已删除'
              : hasCampaign
                ? '普通投放已暂停'
                : '未开启';
      map.set(String(id), {
        campaign,
        campaignId,
        campaignName: automationStats?.latest_campaign_name || campaignNameOf(campaign) || '',
        enabled,
        autoManaged,
        strategyEnabled,
        controlledTest,
        controlledTestActive,
        occupied,
        deleted,
        hasCampaign,
        hasAdvertising,
        recoverable: Boolean(campaignId && !deleted),
        label,
      });
    });
    return map;
  }, [campaignCards, productRows]);

  useEffect(() => {
    let restored = [];
    let hasStoredPreference = false;
    try {
      const stored = window.localStorage.getItem(expandedStorageKey);
      hasStoredPreference = stored !== null;
      const parsed = stored ? JSON.parse(stored) : [];
      if (Array.isArray(parsed)) restored = parsed.map(String);
    } catch (error) {
      console.warn('Unable to restore expanded GMV Max product cards', error);
    }
    const validIds = new Set(productRows.map((product) => String(getProductIdentifier(product) || '')).filter(Boolean));
    const validRestored = restored.filter((id) => validIds.has(id));
    const advertisingIds = productRows
      .map((product) => String(getProductIdentifier(product) || ''))
      .filter((id) => id && statusByProduct.get(id)?.hasAdvertising);
    setExpandedProductOrder([
      ...advertisingIds,
      ...(hasStoredPreference ? validRestored : []),
    ].filter((id, index, values) => values.indexOf(id) === index));
  }, [expandedStorageKey, productRows, statusByProduct]);

  const persistExpandedOrder = useCallback((nextOrder) => {
    setExpandedProductOrder(nextOrder);
    try {
      window.localStorage.setItem(expandedStorageKey, JSON.stringify(nextOrder));
    } catch (error) {
      console.warn('Unable to persist expanded GMV Max product cards', error);
    }
  }, [expandedStorageKey]);

  const toggleProductExpanded = useCallback((productId) => {
    const key = String(productId || '');
    if (!key) return;
    const current = expandedProductOrder.filter((id) => id !== key);
    persistExpandedOrder(expandedProductOrder.includes(key) ? current : [key, ...current]);
  }, [expandedProductOrder, persistExpandedOrder]);

  const displayProductRows = useMemo(() => {
    return productRows
      .map((product, index) => ({ product, index }))
      .sort((left, right) => {
        const leftId = String(getProductIdentifier(left.product) || '');
        const rightId = String(getProductIdentifier(right.product) || '');
        const leftStatus = statusByProduct.get(leftId) || {};
        const rightStatus = statusByProduct.get(rightId) || {};
        const rank = (status) => status.hasAdvertising ? 0 : status.recoverable ? 1 : 2;
        return rank(leftStatus) - rank(rightStatus) || left.index - right.index;
      })
      .map(({ product }) => product);
  }, [productRows, statusByProduct]);
  const updateDailyCap = (productId, value) => {
    setDailyCapByProduct((prev) => ({ ...prev, [String(productId)]: value }));
  };

  const refreshAfterSuccessfulMutation = async () => {
    try {
      await onChanged?.();
    } catch (error) {
      console.warn('GMV Max mutation succeeded but the follow-up refresh failed', error);
      setNotice((current) => {
        if (!current || current.type !== 'success') return current;
        return {
          ...current,
          message: `${current.message} 页面数据刷新稍有延迟，请稍后刷新确认。`,
        };
      });
    }
  };

  const loadCreativeCandidates = async (productId, { refresh = false } = {}) => {
    const productKey = String(productId || '');
    if (!productKey || !canOperate) return;
    setCreativeLoadingByProduct((prev) => ({ ...prev, [productKey]: true }));
    setCreativeErrorByProduct((prev) => ({ ...prev, [productKey]: '' }));
    try {
      const scopeParams = {
        store_id: storeId,
        advertiser_id: advertiserId || undefined,
        item_group_id: productKey,
      };
      if (refresh) {
        await refreshGmvMaxCreativeAssets(
          workspaceId,
          provider,
          authId,
          scopeParams,
        );
      }
      const result = await listGmvMaxCreativeAssets(workspaceId, provider, authId, {
        ...scopeParams,
        lookback_days: 45,
        page_size: 100,
        fetch_all_pages: true,
      });
      const items = [
        ...ensureArray(result?.items),
        ...ensureArray(result?.uploads),
      ];
      setCreativeCandidatesByProduct((prev) => ({ ...prev, [productKey]: items }));
      setCreativeRecommendationByProduct((prev) => ({
        ...prev,
        [productKey]: result?.hermes || null,
      }));
      const recommendedIds = ensureArray(result?.items)
        .filter((item) => item?.selectable && item?.hermes_recommended)
        .slice(0, 4)
        .map(getCreativeId)
        .filter(Boolean);
      setSelectedCreativeByProduct((prev) => {
        if (Object.prototype.hasOwnProperty.call(prev, productKey)) return prev;
        return { ...prev, [productKey]: recommendedIds };
      });
      if (items.length === 0 && refresh) {
        setCreativeErrorByProduct((prev) => ({ ...prev, [productKey]: '暂未从 TikTok 返回可选视频素材。' }));
      }
    } catch (error) {
      setCreativeErrorByProduct((prev) => ({ ...prev, [productKey]: formatError(error) || '素材加载失败' }));
    } finally {
      setCreativeLoadingByProduct((prev) => ({ ...prev, [productKey]: false }));
    }
  };

  const setCreativeMode = (productId, mode) => {
    const productKey = String(productId || '');
    setCreativeModeByProduct((prev) => ({ ...prev, [productKey]: mode }));
    if (mode === CREATIVE_MODE_MANUAL && !creativeCandidatesByProduct[productKey]) {
      loadCreativeCandidates(productKey, { refresh: false });
    }
  };

  const toggleCreativeSelection = (productId, creativeId) => {
    const productKey = String(productId || '');
    const itemKey = String(creativeId || '');
    if (!productKey || !itemKey) return;
    setSelectedCreativeByProduct((prev) => {
      const current = new Set(prev[productKey] || []);
      if (current.has(itemKey)) current.delete(itemKey);
      else current.add(itemKey);
      return { ...prev, [productKey]: Array.from(current) };
    });
  };

  const openUploadDialog = async (productId) => {
    const productKey = String(productId || '');
    setUploadDialog({
      originProductId: productKey,
      productId: productKey,
      tiktokAccountId: '',
      file: null,
      title: '',
    });
    setTiktokAccountsLoading(true);
    setTiktokAccountsError('');
    try {
      const accounts = ensureArray(await listTikTokAccounts(workspaceId));
      setTiktokAccounts(accounts);
      if (accounts.length === 1) {
        setUploadDialog((current) => current
          ? { ...current, tiktokAccountId: String(accounts[0].account_id) }
          : current);
      }
    } catch (error) {
      setTiktokAccounts([]);
      setTiktokAccountsError(formatError(error) || 'TikTok 账号加载失败');
    } finally {
      setTiktokAccountsLoading(false);
    }
  };

  const handleUploadCreative = async () => {
    const productKey = String(uploadDialog?.originProductId || '');
    const selectedProductId = String(uploadDialog?.productId || '');
    const tiktokAccountId = String(uploadDialog?.tiktokAccountId || '');
    const file = uploadDialog?.file;
    if (!productKey || !tiktokAccountId || !file || !canOperate) return;
    setUploadingProductId(productKey);
    setCreativeErrorByProduct((prev) => ({ ...prev, [productKey]: '' }));
    try {
      const formData = new FormData();
      formData.append('store_id', storeId);
      formData.append('tiktok_account_id', tiktokAccountId);
      if (advertiserId) formData.append('advertiser_id', String(advertiserId));
      if (selectedProductId) formData.append('item_group_id', selectedProductId);
      formData.append('title', uploadDialog?.title?.trim() || file.name || '上传视频');
      formData.append('file', file);
      const result = await uploadGmvMaxCreativeAsset(workspaceId, provider, authId, formData);
      setUploadDialog(null);
      await loadCreativeCandidates(productKey, { refresh: true });
      const uploadStatus = result?.status;
      setNotice({
        type: uploadStatus === 'TIKTOK_PUBLISH_FAILED' ? 'error' : 'success',
        message: uploadStatus === 'TIKTOK_PUBLISH_FAILED'
          ? formatCreativeUploadError(result?.not_selectable_reason)
          : selectedProductId
            ? '视频已交给所选 TikTok 账号发布；系统会自动等待审核并关联商品，完成后即可选择投放。'
            : '视频已交给所选 TikTok 账号发布；发布完成后会进入素材池，可在投放时关联商品。',
      });
    } catch (error) {
      setCreativeErrorByProduct((prev) => ({ ...prev, [productKey]: formatError(error) || '视频上传失败' }));
    } finally {
      setUploadingProductId('');
    }
  };

  const handleEnable = async (product, requestedLaunchMode, requestedAction) => {
    const productId = getProductIdentifier(product);
    if (!productId || !canOperate) return;
    const productKey = String(productId);
    const launchMode = requestedLaunchMode || launchModeByProduct[productKey] || LAUNCH_MODE_SMART;
    const automationEnabled = launchMode === LAUNCH_MODE_SMART;
    const status = statusByProduct.get(productKey) || {};
    const scopedCreateIntentStorageKey = buildCampaignCreateIntentKey({
      workspaceId,
      provider,
      authId,
      advertiserId,
      storeId,
      productId,
      launchMode,
    });
    const unresolvedCreateIntent = getStoredCampaignCreateIntent({
      storageKey: scopedCreateIntentStorageKey,
      fallbackIntents: campaignCreateIntentFallbackRef.current,
    });
    const launchAction = unresolvedCreateIntent
      ? ACTION_CREATE
      : requestedAction || launchActionByProduct[productKey] || (
          status.recoverable ? ACTION_RESTORE : ACTION_CREATE
        );
    const shouldCreateCampaign = Boolean(unresolvedCreateIntent) ||
      launchAction === ACTION_CREATE ||
      !status.recoverable;
    const createIntentStorageKey = shouldCreateCampaign
      ? scopedCreateIntentStorageKey
      : null;
    const createIntent = shouldCreateCampaign
      ? unresolvedCreateIntent || getOrCreateCampaignCreateIntent({
          storageKey: createIntentStorageKey,
          campaignName: buildCampaignName(product, productId, launchMode),
          fallbackIntents: campaignCreateIntentFallbackRef.current,
        })
      : null;
    const finalizedCreatePayload = getFinalizedCampaignCreatePayload(createIntent);
    const automationStats = getAutomationStats(product);
    const referencePriceValue = getRecommendedValue(
      product?.effective_price,
      product?.effectivePrice,
      automationStats?.reference_price,
      automationStats?.referencePrice,
    );
    const dailyCapValue = dailyCapByProduct[productKey] ?? getRecommendedValue(
      automationStats?.daily_spend_cap,
      automationStats?.dailySpendCap,
    );
    const referencePrice = parseOptionalFloat(referencePriceValue);
    const manualDailyCap = parseOptionalFloat(dailyCapValue);
    if (!finalizedCreatePayload && automationEnabled && (!referencePrice || referencePrice <= 0)) {
      setNotice({ type: 'error', message: 'TikTok Shop 暂未返回该商品的有效成交价，请先同步店铺商品和订单数据。' });
      return;
    }
    if (
      !finalizedCreatePayload &&
      automationEnabled &&
      dailyCapValue &&
      (!manualDailyCap || manualDailyCap <= 0)
    ) {
      setNotice({ type: 'error', message: '每日消耗上限必须大于 0。' });
      return;
    }
    const creativeMode = launchMode === LAUNCH_MODE_MANUAL
      ? CREATIVE_MODE_MANUAL
      : creativeModeByProduct[productKey] || CREATIVE_MODE_AUTO;
    const selectedCreativeIds = new Set(selectedCreativeByProduct[productKey] || []);
    const creativeCandidates = ensureArray(creativeCandidatesByProduct[productKey]);
    const selectedCreativeAssets = creativeCandidates.filter((asset) => selectedCreativeIds.has(getCreativeId(asset)));
    if (
      !finalizedCreatePayload &&
      shouldCreateCampaign &&
      creativeMode === CREATIVE_MODE_MANUAL &&
      selectedCreativeAssets.filter((asset) => asset?.selectable).length === 0
    ) {
      setNotice({ type: 'error', message: '请选择至少一个带 GMV Max post item_id 的可投放视频。仅上传到素材库的视频暂不能创建手动验证计划。' });
      if (!creativeCandidatesByProduct[productKey]) loadCreativeCandidates(productKey, { refresh: false });
      return;
    }

    setPendingProductId(productKey);
    setNotice(null);
    try {
      if (!shouldCreateCampaign && status.campaignId) {
        if (automationEnabled) {
          const fallbackCap = campaignBudgetAmount(status.campaign);
          const dailySpendCap = manualDailyCap || (fallbackCap ? fallbackCap * 0.8 : 50);
          await updateGmvMaxStrategy(
            workspaceId,
            provider,
            authId,
            status.campaignId,
            buildStrategyPayload(referencePrice, dailySpendCap, productId),
          );
        } else {
          await updateGmvMaxStrategy(workspaceId, provider, authId, status.campaignId, {
            enabled: false,
            auto_heating_enabled: false,
            config_json: {
              hermes_enabled: false,
              smart_guard: { enabled: false },
              creative_guard: { enabled: false },
            },
          });
        }
        if (!status.enabled) {
          await applyGmvMaxAction(workspaceId, provider, authId, status.campaignId, { type: 'enable' });
        }
        setNotice({
          type: 'success',
          message: automationEnabled
            ? status.enabled ? '智能投放参数已保存。' : '原计划已按智能模式恢复。'
            : status.enabled ? '原计划已切换为普通手动管理。' : '原计划已按普通手动模式恢复。',
        });
        setLaunchModeByProduct((prev) => ({ ...prev, [productKey]: '' }));
        setLaunchActionByProduct((prev) => ({ ...prev, [productKey]: '' }));
        await refreshAfterSuccessfulMutation();
        return;
      }

      let createPayload = finalizedCreatePayload;
      if (!createPayload) {
        const storeAuthorizedBcId = resolveStoreAuthorizedBcId(bindingConfig, businessCenterId);
        const precheck = await precheckGmvMaxCampaign(workspaceId, provider, authId, {
          store_id: storeId,
          store_authorized_bc_id: storeAuthorizedBcId,
          bc_id: businessCenterId || undefined,
          advertiser_id: advertiserId || undefined,
          identity_id: null,
          product_specific_type: PRODUCT_SPECIFIC_TYPE,
          item_group_ids: [String(productId)],
        });
        if (precheck?.is_gmv_max_available === false) {
          throw new Error('当前店铺不支持 Product GMV Max。');
        }
        if (precheck?.needs_exclusive_auth) {
          throw new Error('当前授权账号与店铺独占授权不匹配，请检查授权绑定。');
        }

        const identities = ensureArray(precheck?.available_identities)
          .map(normalizeIdentityOption)
          .filter(Boolean);
        const recommendedRoas = getRecommendedValue(
          precheck?.recommended_roas_bid,
          precheck?.recommended_roas,
          2,
        );
        const recommendedBudget = getRecommendedValue(
          precheck?.recommended_budget,
          precheck?.min_budget,
          50,
        );
        const dailySpendCap = manualDailyCap || Number(recommendedBudget) * 0.8;
        const identityPayloads = buildIdentityList(identities, storeId);
        const manualCreativePayload = creativeMode === CREATIVE_MODE_MANUAL
          ? buildManualCreativePayload(selectedCreativeAssets, productId, identityPayloads[0], storeId)
          : { itemList: [], customAnchorVideoList: [] };
        if (creativeMode === CREATIVE_MODE_MANUAL && manualCreativePayload.itemList.length === 0) {
          throw new Error('所选视频缺少 GMV Max post item_id、TikTok 视频 ID 或授权身份，不能创建手动验证计划。');
        }
        createPayload = finalizeCampaignCreateIntent({
          storageKey: createIntentStorageKey,
          intent: createIntent,
          fallbackIntents: campaignCreateIntentFallbackRef.current,
          createPayload: {
            request_id: createIntent.request_id,
            idempotency_key: createIntent.request_id,
            store_id: storeId,
            campaign_name: createIntent.campaign_name,
            product_specific_type: PRODUCT_SPECIFIC_TYPE,
            item_group_ids: [String(productId)],
            roas_bid: Number(recommendedRoas),
            budget: Number(recommendedBudget),
            schedule_type: 'SCHEDULE_FROM_NOW',
            schedule_start_time: toIsoString(new Date()),
            schedule_end_time: null,
            product_video_specific_type: creativeMode,
            identity_list: identityPayloads.length > 0 ? identityPayloads : undefined,
            item_list: creativeMode === CREATIVE_MODE_MANUAL ? manualCreativePayload.itemList : undefined,
            custom_anchor_video_list: creativeMode === CREATIVE_MODE_MANUAL
              ? manualCreativePayload.customAnchorVideoList
              : undefined,
            advertiser_id: advertiserId ? String(advertiserId) : undefined,
            replacement_campaign_id: status.recoverable
              ? String(status.campaignId)
              : undefined,
            automation: {
              enabled: automationEnabled,
              smart_guard_enabled: automationEnabled,
              creative_guard_enabled: automationEnabled,
              auto_heating_enabled: automationEnabled,
              hermes_enabled: automationEnabled,
              cooldown_minutes: 30,
              monitor_interval_minutes: 1,
              evaluation_window_minutes: 60,
              min_runtime_minutes_before_first_change: 10,
              min_spend_cents: 300,
              min_roi: 0.8,
              daily_spend_cap_enabled: automationEnabled,
              daily_spend_cap_cents: automationEnabled
                ? Math.round(dailySpendCap * 100)
                : undefined,
              product_effective_prices: automationEnabled
                ? { [String(productId)]: referencePrice }
                : {},
              creative_selection_mode: creativeMode,
              manual_creative_ids: creativeMode === CREATIVE_MODE_MANUAL
                ? manualCreativePayload.itemList.map((item) => item.item_id)
                : undefined,
            },
          },
        });
      }
      const createResult = await createGmvMaxCampaign(
        workspaceId,
        provider,
        authId,
        createPayload,
      );
      if (['QUARANTINED', 'QUARANTINE_PENDING'].includes(createResult?.creation_status)) {
        const warning = ensureArray(createResult?.warnings)[0];
        setNotice({
          type: 'error',
          message: warning?.message ||
            '系列已经创建，但智能策略初始化或安全暂停尚未完成。请使用同一次新建请求重试恢复，不要重复创建。',
        });
        await refreshAfterSuccessfulMutation();
        return;
      }
      clearCampaignCreateIntent(
        createIntentStorageKey,
        campaignCreateIntentFallbackRef.current,
      );
      setNotice({
        type: 'success',
        message: launchMode === LAUNCH_MODE_MANUAL
          ? '普通手动投放已创建；系统仅同步广告数据，不执行 Hermes 自动暂停、重建或素材排除。'
          : creativeMode === CREATIVE_MODE_MANUAL
            ? '指定视频的智能投放已创建，系统会监控 ROI、消耗和素材表现。'
          : '智能投放已创建，系统会自动监控消耗、ROI 和素材表现，并继承历史排除素材。',
      });
      setLaunchModeByProduct((prev) => ({ ...prev, [productKey]: '' }));
      setLaunchActionByProduct((prev) => ({ ...prev, [productKey]: '' }));
      setLaunchPickerProductId('');
      await refreshAfterSuccessfulMutation();
    } catch (error) {
      if (
        createIntentStorageKey &&
        isDefinitiveCreateRejection(error)
      ) {
        clearCampaignCreateIntent(
          createIntentStorageKey,
          campaignCreateIntentFallbackRef.current,
        );
      }
      setNotice({ type: 'error', message: formatError(error) || '智能投放开启失败，请稍后重试。' });
    } finally {
      setPendingProductId('');
    }
  };

  const handleManualPause = async (status) => {
    if (!status?.campaignId || !canOperate) return;
    setPendingProductId(String(status.campaignId));
    setNotice(null);
    try {
      const result = await applyGmvMaxAction(workspaceId, provider, authId, status.campaignId, { type: 'pause' });
      setNotice({
        type: 'success',
        message: result?.status === 'queued'
          ? '暂停请求已接管，正以最高优先级执行；仅等待当前同账户请求结束。'
          : '普通投放已暂停。',
      });
      await refreshAfterSuccessfulMutation();
    } catch (error) {
      setNotice({ type: 'error', message: formatError(error) || '普通投放暂停失败，请稍后重试。' });
    } finally {
      setPendingProductId('');
    }
  };

  const handleDisable = async (status) => {
    if (!status?.campaignId || !canOperate) return;
    setPendingProductId(String(status.campaignId));
    setNotice(null);
    try {
      const pauseResult = await applyGmvMaxAction(workspaceId, provider, authId, status.campaignId, {
        type: 'pause',
        disable_strategy: true,
      });
      if (pauseResult?.status === 'queued') {
        setNotice({ type: 'success', message: '智能策略已停用；官方暂停请求已接管，正等待当前同账户请求结束。' });
        await refreshAfterSuccessfulMutation();
        return;
      }
      setNotice({ type: 'success', message: '智能投放已关闭，系列已暂停且策略已停用。' });
      await refreshAfterSuccessfulMutation();
    } catch (error) {
      setNotice({ type: 'error', message: formatError(error) || '智能投放关闭失败，请稍后重试。' });
    } finally {
      setPendingProductId('');
    }
  };

  return (
    <section className="gmvmax-card gmvmax-product-automation">
      <header className="gmvmax-card__header">
        <div>
          <h2>商品投放</h2>
          <p>点击商品展开投放详情；未投放商品可选择智能投放或普通手动投放。</p>
          {statsRangeLabel ? <p className="gmvmax-card__subtitle">执行数据：{statsRangeLabel}</p> : null}
        </div>
        <div className="gmvmax-product-automation__controls">
          {productsRefreshing ? <span className="gmvmax-refresh-indicator">数据更新中…</span> : null}
          <div className="gmvmax-date-filters">
            {STATS_RANGE_OPTIONS.map((option) => (
              <button
                key={option.key}
                type="button"
                className={`gmvmax-chip ${statsRangeKey === option.key ? 'gmvmax-chip--active' : ''}`}
                onClick={() => onStatsRangeChange?.(option.key)}
              >
                {option.label}
              </button>
            ))}
            {statsRangeKey === 'custom' ? (
              <div className="gmvmax-date-filters__custom">
                <input
                  type="date"
                  value={statsCustomRange.start}
                  onChange={(event) => onStatsCustomRangeChange?.('start', event.target.value)}
                />
                <span>至</span>
                <input
                  type="date"
                  value={statsCustomRange.end}
                  onChange={(event) => onStatsCustomRangeChange?.('end', event.target.value)}
                />
              </div>
            ) : null}
          </div>
          <div className="gmvmax-product-automation__search">
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索商品名称或 ID"
            />
          </div>
        </div>
      </header>

      {notice ? (
        <div className={`gmvmax-banner gmvmax-banner--${notice.type === 'error' ? 'error' : 'success'}`}>
          {notice.message}
        </div>
      ) : null}

      {productsLoading ? <Loading text="商品加载中..." /> : null}
      {!canOperate ? <p className="gmvmax-placeholder">请选择店铺并完成绑定后再开启智能投放。</p> : null}
      {!productsLoading && productRows.length === 0 ? (
        <p className="gmvmax-placeholder">当前店铺暂无可投放商品。</p>
      ) : null}

      <div className="gmvmax-product-automation__sections">
        {[
          {
            key: 'expanded',
            label: '投放详情',
            rows: displayProductRows.filter((product) =>
              expandedProductOrder.includes(String(getProductIdentifier(product) || '')),
            ),
          },
          {
            key: 'compact',
            label: '其他商品',
            rows: displayProductRows.filter((product) =>
              !expandedProductOrder.includes(String(getProductIdentifier(product) || '')),
            ),
          },
        ].map((section) => (section.rows.length > 0 ? (
          <section key={section.key} className={`gmvmax-product-automation__section gmvmax-product-automation__section--${section.key}`}>
            <header className="gmvmax-product-automation__section-header">
              <strong>{section.label}</strong>
              <span>{section.rows.length} 个商品</span>
            </header>
            <div className={`gmvmax-product-automation__grid gmvmax-product-automation__grid--${section.key}`}>
        {section.rows.map((product) => {
          const productId = getProductIdentifier(product);
          if (!productId) return null;
          const key = String(productId);
          const imageUrl = getProductImage(product);
          const name = getProductName(product, key);
          const price = getProductPrice(product);
          const status = statusByProduct.get(key) || {};
          const automationStats = getAutomationStats(product);
          const metricRows = buildAutomationMetricRows(automationStats);
          const visibleMetricRows = status.autoManaged ? metricRows : metricRows.slice(0, 1);
          const controlReason = automationStats?.latest_control_event?.reason || '';
          const latestReason = formatAutomationReason(
            !status.enabled && controlReason
              ? controlReason
              : automationStats?.latest_event?.reason || automationStats?.last_reason || '',
          );
          const nextReviewAt = getRecommendedValue(
            automationStats?.next_automation_review_at,
            automationStats?.nextAutomationReviewAt,
            automationStats?.next_automation_start_at,
            automationStats?.nextAutomationStartAt,
          );
          const showStartSchedule = Boolean(status.autoManaged && status.strategyEnabled && !status.enabled);
          const nextReviewLabel = status.autoManaged && status.strategyEnabled
            ? formatAutomationStartTime(nextReviewAt, advertiserTimezone)
            : '';
          const reviewScheduleValue = nextReviewLabel || (showStartSchedule ? '等待策略复核' : '');
          const nextReviewReason = formatAutomationReason(
            automationStats?.next_automation_review_reason ||
              automationStats?.nextAutomationReviewReason ||
            automationStats?.next_automation_start_reason ||
              automationStats?.nextAutomationStartReason ||
              automationStats?.last_reason ||
              '',
          );
          const resumeCondition = automationStats?.resume_condition || automationStats?.resumeCondition || '';
          const controlledTest = automationStats?.controlled_test || automationStats?.controlledTest || {};
          const controlledTestActive = Boolean(controlledTest?.active);
          const controlledTestBudget = Number(
            controlledTest?.budget ?? (Number(controlledTest?.budget_cents || 0) / 100),
          );
          const controlledTestSpent = Number(
            controlledTest?.spent ?? (Number(controlledTest?.spent_cents || 0) / 100),
          );
          const controlledTestStage = String(controlledTest?.stage || '').toUpperCase();
          const controlledTestStageLabel = controlledTestStage === 'DELIVERY_PROBE'
            ? '\u6d41\u91cf\u63a2\u9488'
            : controlledTestStage === 'PERFORMANCE_TEST'
              ? '\u6548\u679c\u9a8c\u8bc1'
              : controlledTest?.rebuild_pending
                ? '\u7b49\u5f85\u91cd\u5efa'
                : '';
          const dataConsistency = automationStats?.data_consistency || automationStats?.dataConsistency || {};
          const consistencyState = String(dataConsistency?.state || '').toLowerCase();
          const consistencyLabel = consistencyState === 'consistent'
            ? '数据一致'
            : consistencyState === 'conflict'
              ? '数据冲突'
              : consistencyState === 'degraded'
                ? '部分数据降级'
                : '';
          const hermesReview = automationStats?.hermes_action_review || automationStats?.hermesActionReview || {};
          const hermesDecision = String(hermesReview?.decision || '').toUpperCase();
          const hermesLabel = hermesDecision === 'APPROVE'
            ? 'Hermes 已批准'
            : hermesDecision === 'REVISE'
              ? 'Hermes 已调整'
              : hermesDecision === 'HOLD'
                ? 'Hermes 待观察'
                : hermesReview?.status
                  ? 'Hermes 审核中'
                  : '';
          const isPending = Boolean(pendingProductId) && (
            pendingProductId === key || pendingProductId === String(status.campaignId || '')
          );
          const savedDailyCap = getRecommendedValue(
            automationStats?.daily_spend_cap,
            automationStats?.dailySpendCap,
          );
          const capValue = dailyCapByProduct[key] ?? (savedDailyCap ?? '');
          const isAutomationRunning = status.autoManaged && status.strategyEnabled && status.enabled;
          const launchMode = launchModeByProduct[key] || '';
          const creativeMode = launchMode === LAUNCH_MODE_MANUAL
            ? CREATIVE_MODE_MANUAL
            : creativeModeByProduct[key] || CREATIVE_MODE_AUTO;
          const creativeCandidates = ensureArray(creativeCandidatesByProduct[key]);
          const creativeSearchValue = creativeSearchByProduct[key] || '';
          const normalizedCreativeSearch = creativeSearchValue.trim().toLowerCase();
          const visibleCreativeCandidates = normalizedCreativeSearch
            ? creativeCandidates.filter((asset) => {
              const searchable = [
                getCreativeId(asset),
                getCreativeTitle(asset),
                asset?.creative_name,
                asset?.creativeName,
              ].map((value) => String(value || '').toLowerCase());
              return searchable.some((value) => value.includes(normalizedCreativeSearch));
            })
            : creativeCandidates;
          const selectedCreativeIds = new Set(selectedCreativeByProduct[key] || []);
          const selectedCreativeCount = creativeCandidates.filter((asset) => selectedCreativeIds.has(getCreativeId(asset))).length;
          const creativeLoading = Boolean(creativeLoadingByProduct[key]);
          const creativeError = creativeErrorByProduct[key] || '';
          const creativeRecommendation = creativeRecommendationByProduct[key] || null;
          const uploading = uploadingProductId === key;
          const isExpanded = expandedProductOrder.includes(key);
          const launchAction = launchActionByProduct[key] || '';
          const showSmartParameters = status.autoManaged || launchMode === LAUNCH_MODE_SMART || (
            !status.campaignId && launchMode !== LAUNCH_MODE_MANUAL
          );
          const isIdleProduct = !status.hasAdvertising && !status.recoverable;
          return (
            <article key={key} className={`gmvmax-auto-product-card ${isAutomationRunning ? 'is-active' : ''} ${isExpanded ? 'is-expanded' : ''} ${isIdleProduct ? 'is-idle' : ''}`}>
              <button
                type="button"
                className="gmvmax-auto-product-card__summary"
                onClick={() => toggleProductExpanded(key)}
                aria-expanded={isExpanded}
                aria-label={`${isExpanded ? '收起' : '展开'}商品 ${name}`}
                title={isExpanded ? '收起商品详情' : `展开 ${name}`}
              >
                <span className="gmvmax-auto-product-card__media">
                  {imageUrl ? <img src={imageUrl} alt="" loading="lazy" /> : <span>封面</span>}
                </span>
                <span className="gmvmax-auto-product-card__summary-main">
                  <strong title={name}>{truncateName(name, 48)}</strong>
                  <small>ID {shortId(key)} · {price ? `成交参考价 ${formatMoney(price)}` : '成交参考价待同步'}</small>
                  <span className={`gmvmax-status-pill ${isAutomationRunning ? 'gmvmax-status-pill--success' : status.enabled ? 'gmvmax-status-pill--warning' : 'gmvmax-status-pill--muted'}`}>
                    {status.label}
                  </span>
                </span>
                <span className="gmvmax-auto-product-card__summary-metrics">
                  <small>花费</small>
                  <strong>{metricRows[0]?.[0]?.value || '—'}</strong>
                  <small>GMV</small>
                  <strong>{metricRows[0]?.[1]?.value || '—'}</strong>
                </span>
                <span className="gmvmax-auto-product-card__chevron" aria-hidden="true">⌄</span>
              </button>
              {isExpanded ? (
                <>
              <div className="gmvmax-auto-product-card__body">
                <div className="gmvmax-auto-product-card__status">
                  <span>关联系列</span>
                  <small title={status.campaignName || '暂未关联系列'}>
                    {status.campaignName ? truncateName(status.campaignName, 36) : '暂未关联系列'}
                  </small>
                </div>
                {status.autoManaged && reviewScheduleValue ? (
                  <div className="gmvmax-auto-product-card__schedule" title={nextReviewReason || ''}>
                    <span>最早复核</span>
                    <strong>{reviewScheduleValue}</strong>
                  </div>
                ) : null}
                {status.autoManaged && controlledTestActive ? (
                  <div className="gmvmax-auto-product-card__schedule" title="预算由 Hermes 在平台安全区间内选择">
                    <span>{controlledTestStageLabel || '测试进度'}</span>
                    <strong>{formatMoney(controlledTestSpent)} / {formatMoney(controlledTestBudget)}</strong>
                  </div>
                ) : null}
                {status.autoManaged && !controlledTestActive && controlledTestStageLabel ? (
                  <div className="gmvmax-auto-product-card__schedule">
                    <span>{controlledTestStageLabel}</span>
                    <strong>{controlledTest?.status || '--'}</strong>
                  </div>
                ) : null}
                {status.autoManaged && (consistencyLabel || hermesLabel) ? (
                  <div className="gmvmax-auto-product-card__decision">
                    {consistencyLabel ? (
                      <span className={consistencyState === 'conflict' ? 'is-risk' : ''}>{consistencyLabel}</span>
                    ) : null}
                    {hermesLabel ? <span>{hermesLabel}</span> : null}
                  </div>
                ) : null}
                {status.autoManaged && resumeCondition ? (
                  <div className="gmvmax-auto-product-card__condition" title={resumeCondition}>
                    恢复条件：{resumeCondition}
                  </div>
                ) : null}
                {showSmartParameters ? <div className="gmvmax-auto-product-card__price gmvmax-auto-product-card__price--readonly">
                  <span>参考成交价</span>
                  <strong>{price ? formatMoney(price) : '待同步'}</strong>
                  <small>{getProductPriceSourceLabel(product)}</small>
                </div> : null}
                {showSmartParameters ? <label className="gmvmax-auto-product-card__price">
                  <span>每日上限</span>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={capValue}
                    onChange={(event) => updateDailyCap(key, event.target.value)}
                    placeholder="默认预算80%"
                    disabled={isPending || !canOperate}
                  />
                </label> : null}
              </div>
              <div className="gmvmax-auto-product-card__creative">
                {launchAction === ACTION_RESTORE && launchMode === LAUNCH_MODE_MANUAL ? (
                  <div className="gmvmax-auto-product-card__empty-stats">
                    恢复原计划将保留现有素材与出价，仅关闭 Hermes 自动策略并恢复投放。
                  </div>
                ) : (
                  <>
                <div className="gmvmax-auto-product-card__creative-toolbar">
                  <div className="gmvmax-segmented">
                    <button
                      type="button"
                      className={creativeMode === CREATIVE_MODE_AUTO ? 'is-active' : ''}
                      onClick={() => setCreativeMode(key, CREATIVE_MODE_AUTO)}
                      disabled={isPending || !canOperate}
                      aria-pressed={creativeMode === CREATIVE_MODE_AUTO}
                      title="由系统从所有已授权账号中自动选择素材"
                    >
                      <span>系统选素材</span>
                    </button>
                    <button
                      type="button"
                      className={creativeMode === CREATIVE_MODE_MANUAL ? 'is-active' : ''}
                      onClick={() => setCreativeMode(key, CREATIVE_MODE_MANUAL)}
                      disabled={isPending || !canOperate}
                      aria-pressed={creativeMode === CREATIVE_MODE_MANUAL}
                      title="仅使用你选择的视频创建新的验证计划"
                    >
                      <span>指定视频</span>
                    </button>
                  </div>
                  {creativeMode === CREATIVE_MODE_MANUAL ? (
                    <button
                      type="button"
                      className="gmvmax-button gmvmax-button--ghost"
                      onClick={() => loadCreativeCandidates(key, { refresh: true })}
                      disabled={creativeLoading || isPending || !canOperate}
                    >
                      {creativeLoading ? '同步中...' : '刷新素材'}
                    </button>
                  ) : null}
                </div>
                {creativeMode === CREATIVE_MODE_MANUAL ? (
                  <div className="gmvmax-manual-creatives">
                    {creativeRecommendation ? (
                      <div className="gmvmax-hermes-creative-summary">
                        <strong>Hermes 素材优选</strong>
                        <span>
                          已分析 {formatMetricNumber(creativeRecommendation.evaluated || 0)} 个，
                          推荐 {formatMetricNumber(creativeRecommendation.recommended || 0)} 个
                        </span>
                        <small>
                          {creativeRecommendation.recommended > 0
                            ? creativeRecommendation.recommendation_mode === 'validation'
                              ? '已自动勾选适合小预算验证的视频，可继续人工调整。'
                              : '已自动勾选有成交证据的优质视频，可继续人工调整。'
                            : '暂无达到推荐标准的视频，以下为探索候选，不会自动勾选。'}
                        </small>
                      </div>
                    ) : null}
                    {creativeCandidates.length > 0 ? (
                      <>
                        <label className="gmvmax-manual-creatives__search">
                          <span>搜索视频素材</span>
                          <input
                            type="search"
                            value={creativeSearchValue}
                            placeholder="搜索视频标题或 ID"
                            onChange={(event) => setCreativeSearchByProduct((prev) => ({
                              ...prev,
                              [key]: event.target.value,
                            }))}
                          />
                          <small>
                            显示 {visibleCreativeCandidates.length} / 共 {creativeCandidates.length} 个
                          </small>
                        </label>
                        {visibleCreativeCandidates.length > 0 ? (
                          <div className="gmvmax-manual-creatives__grid">
                            {visibleCreativeCandidates.map((asset) => {
                          const assetId = getCreativeId(asset);
                          const cover = getCreativeCover(asset);
                          const metrics = getCreativeMetrics(asset);
                          const checked = selectedCreativeIds.has(assetId);
                          const disabled = !asset?.selectable || isPending || !canOperate;
                          return (
                            <button
                              key={`${key}-${assetId}`}
                              type="button"
                              className={`gmvmax-manual-creative ${checked ? 'is-selected' : ''} ${!asset?.selectable ? 'is-disabled' : ''}`}
                              onClick={() => !disabled && toggleCreativeSelection(key, assetId)}
                              disabled={disabled}
                              aria-pressed={checked}
                              title={asset?.not_selectable_reason || getCreativeTitle(asset)}
                            >
                              <span className="gmvmax-manual-creative__thumb">
                                {cover ? <img src={cover} alt="" loading="lazy" /> : <span>视频</span>}
                              </span>
                              <span className="gmvmax-manual-creative__body">
                                <span className={`gmvmax-hermes-tier gmvmax-hermes-tier--${String(asset?.hermes_tier || 'unrated').toLowerCase()}`}>
                                  {asset?.hermes_recommended && asset?.hermes_rank
                                    ? `Hermes 推荐 #${asset.hermes_rank}`
                                    : getHermesTierLabel(asset)}
                                </span>
                                <strong>{truncateName(getCreativeTitle(asset), 34)}</strong>
                                <small>
                                  花费 {formatMoney(metrics.spend || 0)} · ROAS {Number(metrics.roi || 0).toFixed(2)} · 订单 {formatMetricNumber(metrics.orders || 0)}
                                </small>
                                {asset?.hermes_reason ? (
                                  <small className="gmvmax-manual-creative__reason">
                                    {truncateName(asset.hermes_reason, 48)}
                                  </small>
                                ) : null}
                              </span>
                            </button>
                          );
                            })}
                          </div>
                        ) : (
                          <div className="gmvmax-auto-product-card__empty-stats">
                            没有匹配“{creativeSearchValue.trim()}”的视频素材。
                          </div>
                        )}
                      </>
                    ) : (
                      <div className="gmvmax-auto-product-card__empty-stats">
                        {creativeLoading ? '正在加载可选视频...' : '暂无候选视频，可先刷新素材。'}
                      </div>
                    )}
                    {creativeError ? <div className="gmvmax-inline-error">{creativeError}</div> : null}
                    <div className="gmvmax-manual-creatives__footer">
                      <span>已选 {selectedCreativeCount} 个视频</span>
                      <button
                        type="button"
                        className={`gmvmax-upload-inline ${uploading ? 'is-loading' : ''}`}
                        disabled={uploading || isPending || !canOperate}
                        onClick={() => openUploadDialog(key)}
                      >
                        {uploading ? '上传中...' : '上传视频'}
                      </button>
                    </div>
                  </div>
                ) : null}
                  </>
                )}
              </div>
              <div className="gmvmax-auto-product-card__stats">
                {metricRows.length > 0 ? (
                  <>
                    {visibleMetricRows.map((row, rowIndex) => (
                      <div key={`${key}-metrics-${rowIndex}`} className="gmvmax-auto-product-card__stats-row">
                        {row.map((item) => (
                          <span key={`${item.label}-${item.value}`}>
                            <small>{item.label}</small>
                            <strong>{item.value}</strong>
                          </span>
                        ))}
                      </div>
                    ))}
                    {status.autoManaged && latestReason ? (
                      <div className="gmvmax-auto-product-card__last" title={latestReason}>
                        最近动作：{truncateName(latestReason, 34)}
                      </div>
                    ) : null}
                  </>
                ) : (
                  <div className="gmvmax-auto-product-card__empty-stats">暂无投放数据</div>
                )}
              </div>
              <div className="gmvmax-auto-product-card__actions">
                {isAutomationRunning ? (
                  <>
                    <button
                      type="button"
                      className="gmvmax-button gmvmax-button--secondary"
                      onClick={() => handleEnable(product)}
                      disabled={isPending || !canOperate}
                    >
                      {isPending ? '保存中...' : '保存参数'}
                    </button>
                    <button
                      type="button"
                      className="gmvmax-button gmvmax-button--danger"
                      onClick={() => handleDisable(status)}
                      disabled={isPending || !canOperate}
                    >
                      {isPending ? '关闭中...' : '关闭智能投放'}
                    </button>
                  </>
                ) : status.enabled && !status.autoManaged ? (
                  <button
                    type="button"
                    className="gmvmax-button gmvmax-button--danger"
                    onClick={() => handleManualPause(status)}
                    disabled={isPending || !canOperate}
                  >
                    {isPending ? '处理中...' : '暂停普通投放'}
                  </button>
                ) : launchMode ? (
                  <>
                    <button
                      type="button"
                      className="gmvmax-button gmvmax-button--ghost"
                      onClick={() => {
                        setLaunchModeByProduct((prev) => ({ ...prev, [key]: '' }));
                        setLaunchActionByProduct((prev) => ({ ...prev, [key]: '' }));
                      }}
                      disabled={isPending}
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      className="gmvmax-button gmvmax-button--primary"
                      onClick={() => handleEnable(product, launchMode, launchAction || ACTION_CREATE)}
                      disabled={isPending || !canOperate}
                    >
                      {isPending
                        ? '处理中...'
                        : launchAction === ACTION_RESTORE
                          ? launchMode === LAUNCH_MODE_MANUAL ? '手动模式恢复' : '智能模式恢复'
                          : launchMode === LAUNCH_MODE_MANUAL ? '新建普通投放' : '新建智能投放'}
                    </button>
                  </>
                ) : (
                  <div className="gmvmax-product-launch">
                    {status.recoverable ? (
                      <button
                        type="button"
                        className="gmvmax-button gmvmax-button--secondary"
                        onClick={() => {
                          setLaunchActionByProduct((prev) => ({ ...prev, [key]: ACTION_RESTORE }));
                          setLaunchPickerProductId(key);
                        }}
                        disabled={isPending || !canOperate}
                      >
                        恢复投放
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="gmvmax-button gmvmax-button--primary"
                      onClick={() => {
                        setLaunchActionByProduct((prev) => ({ ...prev, [key]: ACTION_CREATE }));
                        setLaunchPickerProductId(key);
                      }}
                      disabled={isPending || !canOperate || (status.occupied && !status.campaignId && !status.deleted)}
                    >
                      新建投放
                    </button>
                    {launchPickerProductId === key ? (
                      <div className="gmvmax-product-launch__menu">
                        <button
                          type="button"
                          onClick={() => {
                            setLaunchModeByProduct((prev) => ({ ...prev, [key]: LAUNCH_MODE_SMART }));
                            setCreativeMode(key, CREATIVE_MODE_AUTO);
                            setLaunchPickerProductId('');
                          }}
                        >
                          <strong>{launchAction === ACTION_RESTORE ? '智能模式恢复' : '新建智能投放'}</strong>
                          <small>启用 Hermes 自动监控、止损和素材优化</small>
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setLaunchModeByProduct((prev) => ({ ...prev, [key]: LAUNCH_MODE_MANUAL }));
                            setCreativeMode(key, CREATIVE_MODE_MANUAL);
                            setLaunchPickerProductId('');
                          }}
                        >
                          <strong>{launchAction === ACTION_RESTORE ? '手动模式恢复' : '新建普通投放'}</strong>
                          <small>{launchAction === ACTION_RESTORE ? '恢复原计划并关闭自动策略' : '人工选择视频，系统只同步投放数据'}</small>
                        </button>
                      </div>
                    ) : null}
                  </div>
                )}
              </div>
                </>
              ) : null}
            </article>
          );
        })}
            </div>
          </section>
        ) : null))}
      </div>
      {uploadDialog ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => !uploadingProductId && setUploadDialog(null)}>
          <div
            className="modal gmvmax-upload-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="gmvmax-upload-title"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="modal__header">
              <div>
                <div id="gmvmax-upload-title" className="modal__title">上传投放视频</div>
                <small>发布到 TikTok 账号后进入 GMV Max 素材池</small>
              </div>
              <button
                type="button"
                className="modal__close"
                aria-label="关闭"
                disabled={Boolean(uploadingProductId)}
                onClick={() => setUploadDialog(null)}
              >
                ×
              </button>
            </div>
            <div className="modal__body">
              <div className="gmvmax-upload-dialog__form">
                <label>
                  <span>TikTok 账号 <b aria-hidden="true">*</b></span>
                  <select
                    value={uploadDialog.tiktokAccountId}
                    disabled={tiktokAccountsLoading || Boolean(uploadingProductId)}
                    onChange={(event) => setUploadDialog((current) => ({
                      ...current,
                      tiktokAccountId: event.target.value,
                    }))}
                  >
                    <option value="">{tiktokAccountsLoading ? '正在加载账号...' : '请选择已授权账号'}</option>
                    {tiktokAccounts.map((account) => (
                      <option key={account.account_id} value={account.account_id}>
                        {account.alias || 'TikTok 账号'} · {shortId(account.open_id)}
                      </option>
                    ))}
                  </select>
                </label>
                {tiktokAccountsError ? <div className="gmvmax-inline-error">{tiktokAccountsError}</div> : null}
                {!tiktokAccountsLoading && tiktokAccounts.length === 0 ? (
                  <div className="gmvmax-banner gmvmax-banner--warning">
                    尚未授权可发布视频的 TikTok 账号。
                    <a href={`/tenants/${encodeURIComponent(workspaceId)}/tiktok-business`}>前往账号授权</a>
                  </div>
                ) : null}
                <label>
                  <span>关联商品 <small>可选</small></span>
                  <select
                    value={uploadDialog.productId}
                    disabled={Boolean(uploadingProductId)}
                    onChange={(event) => setUploadDialog((current) => ({
                      ...current,
                      productId: event.target.value,
                    }))}
                  >
                    <option value="">暂不关联商品</option>
                    {ensureArray(products).filter(isProductAvailable).map((product) => {
                      const productId = String(getProductIdentifier(product) || '');
                      return (
                        <option key={productId} value={productId}>
                          {getProductName(product, productId)}
                        </option>
                      );
                    })}
                  </select>
                </label>
                <label>
                  <span>视频标题</span>
                  <input
                    type="text"
                    maxLength={2200}
                    value={uploadDialog.title}
                    placeholder="默认使用文件名"
                    disabled={Boolean(uploadingProductId)}
                    onChange={(event) => setUploadDialog((current) => ({
                      ...current,
                      title: event.target.value,
                    }))}
                  />
                </label>
                <label className="gmvmax-upload-dialog__file">
                  <span>视频文件 <b aria-hidden="true">*</b></span>
                  <input
                    type="file"
                    accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm,.m4v"
                    disabled={Boolean(uploadingProductId)}
                    onChange={(event) => setUploadDialog((current) => ({
                      ...current,
                      file: event.target.files?.[0] || null,
                    }))}
                  />
                  <small>{uploadDialog.file?.name || '请选择 MP4、MOV 或 WebM 文件'}</small>
                </label>
              </div>
              <div className="gmvmax-modal-footer">
                <button
                  type="button"
                  className="gmvmax-button gmvmax-button--ghost"
                  disabled={Boolean(uploadingProductId)}
                  onClick={() => setUploadDialog(null)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="gmvmax-button gmvmax-button--primary"
                  disabled={
                    Boolean(uploadingProductId)
                    || !uploadDialog.tiktokAccountId
                    || !uploadDialog.file
                    || !canOperate
                  }
                  onClick={handleUploadCreative}
                >
                  {uploadingProductId ? '正在发布...' : '上传并发布'}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
