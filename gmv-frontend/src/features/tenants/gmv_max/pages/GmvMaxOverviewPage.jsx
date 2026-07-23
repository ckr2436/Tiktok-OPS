import { Component, useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  useGmvMaxCampaignQuery,
  useGmvMaxConfigQuery,
  useGmvMaxHermesDailyReportsQuery,
  useGmvMaxMetricsQuery,
  useGmvMaxOptionsQuery,
  useGmvMaxBindingStatusQuery,
  useGmvMaxRebindAutoMutation,
  useGmvMaxSyncIntervalQuery,
  useProductsQuery,
  useSyncAccountMetadataMutation,
  useSyncAccountProductsMutation,
  useSyncAdvertiserBalanceMutation,
  useUpdateGmvMaxSyncIntervalMutation,
  useUpdateGmvMaxCampaignMutation,
  useUpdateGmvMaxStrategyMutation,
} from '../hooks/gmvMaxQueries.js';
import {
  getGmvMaxOptions,
} from '../api/gmvMaxApi.js';
import { loadScope, saveScope } from '../utils/scopeStorage.js';
import { loadOverviewRange, saveOverviewRange } from '../utils/overviewRangeStorage.js';

import {
  PROVIDER,
  PROVIDER_LABEL,
  DEFAULT_REPORT_METRICS,
  EMPTY_QUERY_PARAMS,
  DEFAULT_SCOPE,
  formatError,
  isSyncRateLimitedError,
  formatISODate,
  getProductIdentifier,
  normalizeIdValue,
  shouldFetchGmvMaxSeries,
  addId,
  ensureIdSet,
  collectBusinessCenterIdsFromCampaign,
  collectBusinessCenterIdsFromDetail,
  collectAdvertiserIdsFromCampaign,
  collectAdvertiserIdsFromDetail,
  collectStoreIdsFromCampaign,
  collectStoreIdsFromDetail,
  addProductIdentifier,
  buildScopeMatchResult,
  matchesBusinessCenter,
  matchesAdvertiser,
  matchesStore,
  matchesCampaignScope,
  ensureArray,
  getOptionLabel,
  getBusinessCenterId,
  getBusinessCenterLabel,
  getAdvertiserBusinessCenterId,
  collectStoreBusinessCenterCandidates,
  getAdvertiserId,
  getAdvertiserLabel,
  getStoreId,
  getStoreAdvertiserId,
  getStoreLabel,
  normalizeLinksMap,
  extractLinkMap,
  normalizeStatusValue,
  isCampaignDeleted,
  filterCampaignsByStatus,
  parseOptionalFloat,
  formatMoney,
  formatRoi,
  getCampaignStatusMeta,
  isCampaignEnabledStatus,
  extractProductsFromDetail,
  setsEqual,
  toChoiceList,
  extractChoiceList,
} from './gmvMaxOverview/helpers.js';
import { GmvMaxMetricsLevel } from '../constants/metrics.js';
import * as timezoneUtils from '../utils/timezone.js';
import { SeriesErrorNotice } from './gmvMaxOverview/ErrorHandling.jsx';
import ProductSelectionPanel from './gmvMaxOverview/ProductSelectionPanel.jsx';
import CreateSeriesModal from './gmvMaxOverview/CreateSeriesModal.jsx';
import EditSeriesModal from './gmvMaxOverview/EditSeriesModal.jsx';
import ProductAutomationPanel from './gmvMaxOverview/ProductAutomationPanel.jsx';
import OrderIntelligencePanel from './gmvMaxOverview/OrderIntelligencePanel.jsx';
import { GmvMaxTexts } from '../locale.js';
import { useGmvSyncTask } from '../hooks/useGmvSyncTask.js';

class GmvMaxPageErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
    this.handleRetry = this.handleRetry.bind(this);
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error('GMV Max overview failed to render', error, errorInfo);
  }

  handleRetry() {
    this.setState({ hasError: false, error: null });
    this.props.onReset?.();
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="gmvmax-error-card" role="alert">
          <div>
            <h3>GMV Max 模块出现异常</h3>
            <p>请刷新页面或重试同步。如果问题持续，请联系技术支持。</p>
          </div>
          <button
            type="button"
            className="gmvmax-button gmvmax-button--primary"
            onClick={this.handleRetry}
          >
            重试
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

const bindingConfigMatchesScope = (config, { storeId, businessCenterId, advertiserId }) => {
  if (!config || !storeId) return false;
  const normalizedStore = String(config.store_id || '');
  const normalizedBc = config.bc_id ? String(config.bc_id) : '';
  const normalizedAdvertiser = config.advertiser_id ? String(config.advertiser_id) : '';
  if (!normalizedStore || normalizedStore !== String(storeId)) return false;
  if (businessCenterId && normalizedBc !== String(businessCenterId)) return false;
  if (advertiserId && normalizedAdvertiser !== String(advertiserId)) return false;
  return Boolean(normalizedBc && normalizedAdvertiser && normalizedStore);
};

const AUTO_REFRESH_OPTIONS = [10, 15, 20, 30];
const DEFAULT_AUTO_REFRESH_INTERVAL = AUTO_REFRESH_OPTIONS[0];
const BALANCE_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
const OVERVIEW_RANGE_OPTIONS = [
  { key: 'today', label: '今日' },
  { key: 'yesterday', label: '昨天' },
  { key: '7d', label: '近7天' },
  { key: '14d', label: '近14天' },
  { key: '30d', label: '近30天' },
  { key: 'custom', label: '自定义' },
];

function computeOverviewRange(rangeKey, customRange, timeZone) {
  const normalizedTz = timezoneUtils.resolveTimezoneLabel(timeZone);
  const getAdvertiserTodayRange = timezoneUtils.getAdvertiserTodayRange
    || ((tz) => {
      const now = new Date();
      const start = new Date(now);
      start.setUTCHours(0, 0, 0, 0);
      return { start, end: now, timeZone: tz || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' };
    });
  const todayRange = getAdvertiserTodayRange(normalizedTz);

  if (rangeKey === 'custom') {
    if (customRange?.start && customRange?.end) {
      return { start_date: customRange.start, end_date: customRange.end };
    }
    return { start_date: '', end_date: '' };
  }

  if (rangeKey === 'yesterday') {
    const start = new Date(todayRange.start);
    start.setUTCDate(start.getUTCDate() - 1);
    const end = new Date(todayRange.start);
    end.setUTCDate(end.getUTCDate() - 1);
    return timezoneUtils.formatRangeAsDateStrings({ start, end, timeZone: normalizedTz });
  }

  if (rangeKey === '7d') {
    return timezoneUtils.formatRangeAsDateStrings(
      timezoneUtils.getAdvertiserRecentRange(7, normalizedTz),
    );
  }
  if (rangeKey === '14d') {
    return timezoneUtils.formatRangeAsDateStrings(
      timezoneUtils.getAdvertiserRecentRange(14, normalizedTz),
    );
  }
  if (rangeKey === '30d') {
    return timezoneUtils.formatRangeAsDateStrings(
      timezoneUtils.getAdvertiserRecentRange(30, normalizedTz),
    );
  }

  return timezoneUtils.formatRangeAsDateStrings(todayRange);
}

export function formatOverviewRangeLabel(
  rangeKey,
  rangeParams,
  timeZone,
  hasAdvertiserTimezone = false,
) {
  const start = rangeParams?.start_date || '';
  const end = rangeParams?.end_date || '';
  if (!start || !end) return '';
  const prefix = hasAdvertiserTimezone
    ? `按广告主时区 ${timeZone || 'UTC'}`
    : `广告主时区未知，暂按浏览器时区 ${timeZone || 'UTC'}`;
  if (start === end) {
    if (rangeKey === 'today') return `${prefix}：当天 ${start}`;
    if (rangeKey === 'yesterday') return `${prefix}：昨天 ${start}`;
    return `${prefix}：${start}`;
  }
  if (rangeKey === '7d') return `${prefix}：近7天 ${start} 至 ${end}`;
  if (rangeKey === '14d') return `${prefix}：近14天 ${start} 至 ${end}`;
  if (rangeKey === '30d') return `${prefix}：近30天 ${start} 至 ${end}`;
  return `${prefix}：${start} 至 ${end}`;
}

function normalizeReportList(payload) {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.list)) return payload.list;
  if (Array.isArray(payload.data?.list)) return payload.data.list;
  return [];
}

function getReportRecommendationItems(report) {
  const recommendation = report?.recommendation;
  if (!recommendation || typeof recommendation !== 'object') return [];
  const directItems = recommendation.items || recommendation.actions || recommendation.recommendations;
  if (Array.isArray(directItems)) return directItems;
  return Object.entries(recommendation)
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(([key, value]) => ({
      title: key,
      detail: typeof value === 'string' ? value : JSON.stringify(value),
    }));
}

function formatDailyReportStatus(value) {
  const normalized = String(value || '').toUpperCase();
  if (normalized === 'GENERATED' || normalized === 'SUCCESS') return '已生成';
  if (normalized === 'GENERATING' || normalized === 'PENDING') return '生成中';
  if (normalized === 'FAILED') return '生成失败';
  return normalized || '已生成';
}

function formatRecommendationText(item) {
  if (!item) return '';
  if (typeof item === 'string') return item;
  const title = item.title || item.action || item.type || item.name || '';
  const detail = item.detail || item.reason || item.description || item.message || '';
  return [title, detail].filter(Boolean).join('：') || JSON.stringify(item);
}

function centsToAmount(value) {
  if (value === null || value === undefined) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric / 100 : null;
}

function coerceNumber(value) {
  if (value === null || value === undefined) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function normalizeOverviewSummary({ spend, gmv, orders }) {
  const spendValue = coerceNumber(spend);
  const gmvValue = coerceNumber(gmv);
  const ordersValue = coerceNumber(orders);
  const roasValue = spendValue && spendValue > 0 && gmvValue !== null ? gmvValue / spendValue : null;
  const costPerOrderValue = spendValue !== null && ordersValue && ordersValue > 0
    ? spendValue / ordersValue
    : null;

  return {
    spend: spendValue,
    gmv: gmvValue,
    roas: roasValue,
    orders: ordersValue,
    costPerOrder: costPerOrderValue,
  };
}

function deriveOverviewSnapshotSummary(payload) {
  if (!payload) return null;

  const report = payload.report || payload.data?.report || payload.data;
  const snapshot = payload.snapshot || payload.data?.snapshot || payload.data;
  const summarySource = report?.summary || snapshot || payload;

  if (summarySource && typeof summarySource === 'object') {
    const spendValue = coerceNumber(
      summarySource.cost ?? summarySource.spend ?? summarySource.net_cost,
    );
    const gmvValue = coerceNumber(summarySource.gmv ?? summarySource.gross_revenue);
    const ordersValue = coerceNumber(summarySource.orders);

    const hasDirectFields = [
      spendValue,
      gmvValue,
      ordersValue,
    ].some((value) => value !== null);

    if (hasDirectFields) {
      return normalizeOverviewSummary({ spend: spendValue, gmv: gmvValue, orders: ordersValue });
    }
  }

  const snapshotCandidate = snapshot || report?.summary || payload;
  const hasSnapshotFields = snapshotCandidate
    && [
      'cost_cents',
      'net_cost_cents',
      'gross_revenue_cents',
      'orders',
      'roi',
      'cost_per_order',
    ].some((key) => key in snapshotCandidate);

  if (hasSnapshotFields) {
    const spendValue = centsToAmount(
      snapshotCandidate.cost_cents ?? snapshotCandidate.net_cost_cents,
    );
    const gmvValue = centsToAmount(snapshotCandidate.gross_revenue_cents);
    return normalizeOverviewSummary({
      spend: spendValue,
      gmv: gmvValue,
      orders: snapshotCandidate.orders,
    });
  }

  return null;
}

function formatDataFreshness(freshness) {
  if (!freshness) return null;
  const ageSeconds = Number(freshness.age_seconds);
  const ageLabel = Number.isFinite(ageSeconds)
    ? ageSeconds < 60
      ? `${Math.max(0, Math.round(ageSeconds))} 秒`
      : `${Math.max(1, Math.round(ageSeconds / 60))} 分钟`
    : '未知';
  const stateLabels = {
    fresh: '数据新鲜',
    stale: '数据已滞后',
    missing: '暂无快照',
    historical: '历史完整日',
  };
  const sourceLabels = {
    gmv_overview_snapshots: '整体投放数据',
    gmvmax_product_campaign_metrics_daily: '系列投放数据',
  };
  const sourceLabel = sourceLabels[freshness.source] || '投放数据';
  return {
    state: freshness.state || 'missing',
    label: stateLabels[freshness.state] || '状态未知',
    detail: `${sourceLabel} · ${ageLabel}前更新`,
  };
}

export default function GmvMaxOverviewPage() {
  const { wid: workspaceId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const provider = PROVIDER;
  const [autoRefreshInterval, setAutoRefreshInterval] = useState(DEFAULT_AUTO_REFRESH_INTERVAL);
  const [scope, setScope] = useState(() => ({ ...DEFAULT_SCOPE }));
  const [isCreateModalOpen, setCreateModalOpen] = useState(false);
  const [editingCampaignId, setEditingCampaignId] = useState('');
  const [syncError, setSyncError] = useState(null);
  const [intervalNotice, setIntervalNotice] = useState(null);
  const [isSyncing, setIsSyncing] = useState(false);
  const [syncNotice, setSyncNotice] = useState(null);
  const [advertiserTimezone, setAdvertiserTimezone] = useState(() => timezoneUtils.resolveTimezoneLabel());
  const [includeDeletedCampaigns, setIncludeDeletedCampaigns] = useState(false);
  const [seriesStatusFilter, setSeriesStatusFilter] = useState('running');
  const [seriesStoreFilter, setSeriesStoreFilter] = useState('');
  const [seriesSearch, setSeriesSearch] = useState('');
  const [sortOption, setSortOption] = useState('latest');
  const [selectedProductIds, setSelectedProductIds] = useState(() => new Set());
  const [hasLoadedScope, setHasLoadedScope] = useState(false);
  const [overviewRangeKey, setOverviewRangeKey] = useState('today');
  const [overviewCustomRange, setOverviewCustomRange] = useState({ start: '', end: '' });
  const [automationStatsRangeKey, setAutomationStatsRangeKey] = useState('today');
  const [automationStatsCustomRange, setAutomationStatsCustomRange] = useState({ start: '', end: '' });
  const [selectedDailyReportDate, setSelectedDailyReportDate] = useState('');
  const autoOptionsRefreshAccounts = useRef(new Set());
  const syncInFlightRef = useRef(false);
  const balanceRefreshIntervalRef = useRef();

  const authId = scope.accountAuthId ? String(scope.accountAuthId) : '';
  const businessCenterId = scope.bcId ? String(scope.bcId) : '';
  const advertiserId = scope.advertiserId ? String(scope.advertiserId) : '';
  const storeId = scope.storeId ? String(scope.storeId) : '';
  const isScopeReady = Boolean(authId && businessCenterId && advertiserId && storeId);
  const autoRefreshMs = useMemo(
    () => (autoRefreshInterval ? autoRefreshInterval * 60 * 1000 : undefined),
    [autoRefreshInterval],
  );
  const scopeOptionsParams = EMPTY_QUERY_PARAMS;
  const scopeOptionsQueryKey = useMemo(
    () => ['gmvMax', 'options', workspaceId, provider, authId, scopeOptionsParams],
    [authId, provider, scopeOptionsParams, workspaceId],
  );
  const accountsQueryKey = useMemo(
    () => ['gmvMax', 'accounts', workspaceId, provider, EMPTY_QUERY_PARAMS],
    [provider, workspaceId],
  );

  const syncIntervalQuery = useGmvMaxSyncIntervalQuery(workspaceId, provider, authId, {
    enabled: Boolean(workspaceId && provider && authId),
  });

  const refreshIntervalOptions = useMemo(() => {
    const fromApi = syncIntervalQuery.data?.available;
    if (Array.isArray(fromApi)) {
      const normalized = fromApi
        .map((value) => Number(value))
        .filter((value) => AUTO_REFRESH_OPTIONS.includes(value));
      if (normalized.length > 0) {
        return normalized;
      }
    }
    return AUTO_REFRESH_OPTIONS;
  }, [syncIntervalQuery.data?.available]);

  useEffect(() => {
    const interval = syncIntervalQuery.data?.interval;
    if (interval) {
      const normalized = Number(interval);
      if (refreshIntervalOptions.includes(normalized) && normalized !== autoRefreshInterval) {
        setAutoRefreshInterval(normalized);
      }
    }
  }, [autoRefreshInterval, refreshIntervalOptions, syncIntervalQuery.data?.interval]);

  useEffect(() => {
    const handleHttpError = (event) => {
      const url = event?.detail?.context?.error?.config?.url || '';
      if (typeof url === 'string' && url.includes('/gmvmax/binding/auto')) {
        event.preventDefault();
      }
    };
    window.addEventListener('http:error', handleHttpError);
    return () => {
      window.removeEventListener('http:error', handleHttpError);
    };
  }, []);

  useEffect(() => {
    if (!workspaceId) {
      setScope({ ...DEFAULT_SCOPE });
      setHasLoadedScope(false);
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

  useEffect(() => {
    const savedRange = loadOverviewRange(workspaceId, provider, authId, storeId);
    if (savedRange) {
      setOverviewRangeKey(savedRange.rangeKey || 'today');
      setOverviewCustomRange(savedRange.customRange || { start: '', end: '' });
    } else {
      setOverviewRangeKey('today');
      setOverviewCustomRange({ start: '', end: '' });
    }
  }, [authId, provider, storeId, workspaceId]);

  useEffect(() => {
    if (!workspaceId || !provider || !authId || !storeId) return;
    saveOverviewRange(workspaceId, provider, authId, storeId, {
      rangeKey: overviewRangeKey,
      customRange: overviewCustomRange,
    });
  }, [
    authId,
    overviewCustomRange,
    overviewRangeKey,
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

  const accounts = useMemo(() => {
    const items = accountsQuery.data?.items || accountsQuery.data?.list || accountsQuery.data || [];
    return Array.isArray(items) ? items : [];
  }, [accountsQuery.data]);

  const allAccountOptionQueries = useQueries({
    queries: accounts.map((account) => {
      const accountAuthId = account?.auth_id ?? account?.authId;
      return {
        queryKey: ['gmvMax', 'options', workspaceId, provider, String(accountAuthId || ''), scopeOptionsParams],
        queryFn: ({ signal }) =>
          getGmvMaxOptions(workspaceId, provider, accountAuthId, scopeOptionsParams, { signal }),
        enabled: Boolean(workspaceId && provider && accountAuthId),
        staleTime: 5 * 60 * 1000,
      };
    }),
  });

  const bindingConfigQuery = useGmvMaxConfigQuery(
    workspaceId,
    provider,
    authId,
    {
      enabled: Boolean(workspaceId && provider && authId),
      refetchInterval: autoRefreshMs,
    },
  );

  const bindingConfig = bindingConfigQuery.data || null;
  const bindingConfigLoading = bindingConfigQuery.isLoading;
  const bindingConfigFetching = bindingConfigQuery.isFetching;
  const bindingConfigError = bindingConfigQuery.error;
  const bindingConfigPending = !bindingConfig && (bindingConfigLoading || bindingConfigFetching);
  const savedBusinessCenterId = bindingConfig?.bc_id ? String(bindingConfig.bc_id) : '';
  const savedAdvertiserId = bindingConfig?.advertiser_id ? String(bindingConfig.advertiser_id) : '';
  const savedStoreId = bindingConfig?.store_id ? String(bindingConfig.store_id) : '';
  const advertiserBalance = bindingConfig?.advertiser_balance || null;
  const bindingConfigMatchedScope = bindingConfigMatchesScope(bindingConfig, {
    storeId,
    businessCenterId,
    advertiserId,
  });

  const bindingStatusParams = useMemo(() => (storeId ? { store_id: storeId } : {}), [storeId]);
  const bindingStatusQuery = useGmvMaxBindingStatusQuery(
    workspaceId,
    provider,
    authId,
    bindingStatusParams,
    {
      enabled: Boolean(workspaceId && provider && authId && storeId),
    },
  );
  const bindingStatus = bindingStatusQuery.data || null;
  const bindingReady = Boolean(bindingStatus?.binding_ready);
  const bindingStatusRefreshing = bindingStatusQuery.isLoading || bindingStatusQuery.isFetching;
  const bindingErrorMessage = useMemo(() => {
    if (!bindingReady && bindingStatus?.error_message) {
      return bindingStatus.error_message;
    }
    if (bindingStatusQuery.error) {
      return formatError(bindingStatusQuery.error);
    }
    return '';
  }, [bindingReady, bindingStatus?.error_message, bindingStatusQuery.error]);

  useEffect(() => {
    if (!bindingConfig || !savedStoreId) return;
    if (storeId && storeId !== savedStoreId) return;

    setScope((prev) => {
      const nextStoreId = savedStoreId || prev.storeId;
      const nextBcId = savedBusinessCenterId || prev.bcId;
      const nextAdvertiserId = savedAdvertiserId || prev.advertiserId;

      if (
        prev.storeId === nextStoreId &&
        prev.bcId === nextBcId &&
        prev.advertiserId === nextAdvertiserId
      ) {
        return prev;
      }

      return {
        ...prev,
        storeId: nextStoreId || null,
        bcId: nextBcId || null,
        advertiserId: nextAdvertiserId || null,
      };
    });
  }, [
    advertiserId,
    bindingConfig,
    savedAdvertiserId,
    savedBusinessCenterId,
    savedStoreId,
    storeId,
  ]);

  const scopeOptions = scopeOptionsQuery.data || {};
  const scopeOptionsReady = scopeOptionsQuery.isSuccess;

  const aggregatedStoreOptions = useMemo(() => {
    const combined = new Map();
    accounts.forEach((account, index) => {
      const accountAuthId = normalizeIdValue(account?.auth_id ?? account?.authId);
      if (!accountAuthId) return;
      const optionQuery = allAccountOptionQueries[index];
      const optionData = optionQuery?.data || {};
      const advertisers = ensureArray(optionData.advertisers || optionData.advertiser_list);
      const stores = ensureArray(optionData.stores || optionData.store_list);
      if (stores.length === 0) return;

      const bcLinks = extractLinkMap(optionData.links || {}, 'bc_to_advertisers', 'bcToAdvertisers');
      const advertiserLinks = extractLinkMap(
        optionData.links || {},
        'advertiser_to_stores',
        'advertiserToStores',
      );

      const advertiserById = new Map();
      advertisers.forEach((adv) => {
        const id = getAdvertiserId(adv);
        if (id) {
          advertiserById.set(id, adv);
        }
      });

      const advertiserToBusinessCenter = new Map();
      bcLinks.forEach((advIds, bcId) => {
        advIds.forEach((advId) => {
          if (advId && !advertiserToBusinessCenter.has(advId)) {
            advertiserToBusinessCenter.set(advId, bcId);
          }
        });
      });
      advertisers.forEach((adv) => {
        const advId = getAdvertiserId(adv);
        const bcId = getAdvertiserBusinessCenterId(adv);
        if (advId && bcId && !advertiserToBusinessCenter.has(advId)) {
          advertiserToBusinessCenter.set(advId, bcId);
        }
      });

      stores.forEach((store) => {
        const storeId = getStoreId(store);
        if (!storeId) return;
        const candidates = new Set();
        const directAdvertiser = getStoreAdvertiserId(store);
        if (directAdvertiser) {
          candidates.add(directAdvertiser);
        }
        const linked = advertiserLinks.get(storeId) || [];
        linked.forEach((candidate) => {
          if (candidate) {
            candidates.add(candidate);
          }
        });

        let selectedAdvertiserId = '';
        let selectedStatus = '';
        for (const advId of candidates) {
          const advertiser = advertiserById.get(advId);
          const status = normalizeStatusValue(
            advertiser?.authorization_status || advertiser?.auth_status || advertiser?.status,
          );
          if (!selectedAdvertiserId) {
            selectedAdvertiserId = advId;
            selectedStatus = status;
          }
          if (status === 'EFFECTIVE') {
            selectedAdvertiserId = advId;
            selectedStatus = status;
            break;
          }
        }

        let bcId = collectStoreBusinessCenterCandidates(store)[0] || '';
        if (!bcId && selectedAdvertiserId) {
          bcId = advertiserToBusinessCenter.get(selectedAdvertiserId) || '';
        }

        const option = {
          value: storeId,
          label: getStoreLabel(store),
          authId: accountAuthId,
          advertiserId: selectedAdvertiserId,
          bcId: bcId || '',
          advertiserStatus: selectedStatus,
          needsAuthorization: selectedStatus !== 'EFFECTIVE',
        };

        const existing = combined.get(storeId);
        if (!existing) {
          combined.set(storeId, option);
          return;
        }
        if (existing.advertiserStatus === 'EFFECTIVE') return;
        if (option.advertiserStatus === 'EFFECTIVE') {
          combined.set(storeId, option);
          return;
        }
        if (!existing.advertiserId && option.advertiserId) {
          combined.set(storeId, option);
        }
      });
    });
    return Array.from(combined.values());
  }, [accounts, allAccountOptionQueries]);

  const advertiserList = useMemo(() => {
    return ensureArray(scopeOptions.advertisers || scopeOptions.advertiser_list);
  }, [scopeOptions]);

  const storeList = useMemo(() => {
    return ensureArray(scopeOptions.stores || scopeOptions.store_list);
  }, [scopeOptions]);

  const advertiserById = useMemo(() => {
    const map = new Map();
    advertiserList.forEach((adv) => {
      const id = getAdvertiserId(adv);
      if (id) {
        map.set(String(id), adv);
      }
    });
    return map;
  }, [advertiserList]);

  const links = scopeOptions.links || {};
  const bcToAdvertisers = useMemo(
    () => extractLinkMap(links, 'bc_to_advertisers', 'bcToAdvertisers'),
    [links],
  );
  const advertiserToStores = useMemo(
    () => extractLinkMap(links, 'advertiser_to_stores', 'advertiserToStores'),
    [links],
  );

  const businessCenterOptions = useMemo(() => {
    if (!authId) return [];
    const list = ensureArray(
      scopeOptions.bcs ||
        scopeOptions.business_centers ||
        scopeOptions.businessCenters ||
        scopeOptions.bc_list,
    );
    const options = [];
    const seen = new Set();
    const addOptionIfMissing = (value, label) => {
      const normalized = normalizeIdValue(value);
      if (!normalized || seen.has(normalized)) return;
      seen.add(normalized);
      options.push({ value: normalized, label: label || normalized });
    };

    list.forEach((bc) => {
      const id = getBusinessCenterId(bc);
      if (!id) return;
      addOptionIfMissing(id, getBusinessCenterLabel(bc));
    });

    bcToAdvertisers.forEach((_, bcId) => addOptionIfMissing(bcId));
    advertiserList.forEach((adv) => {
      const candidate = getAdvertiserBusinessCenterId(adv);
      if (candidate) {
        addOptionIfMissing(candidate);
      }
    });
    storeList.forEach((store) => {
      collectStoreBusinessCenterCandidates(store).forEach((candidate) => {
        addOptionIfMissing(candidate);
      });
    });
    if (savedBusinessCenterId) {
      addOptionIfMissing(savedBusinessCenterId);
    }
    return options;
  }, [
    advertiserList,
    authId,
    bcToAdvertisers,
    savedBusinessCenterId,
    scopeOptions,
    storeList,
  ]);

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

  const advertiserToBusinessCenter = useMemo(() => {
    const map = new Map();
    bcToAdvertisers.forEach((advs, bcId) => {
      advs.forEach((advId) => {
        if (advId && !map.has(advId)) {
          map.set(advId, bcId);
        }
      });
    });
    advertiserList.forEach((adv) => {
      const advId = getAdvertiserId(adv);
      const bcId = getAdvertiserBusinessCenterId(adv);
      if (advId && bcId && !map.has(advId)) {
        map.set(advId, bcId);
      }
    });
    return map;
  }, [advertiserList, bcToAdvertisers]);

  const storeToAdvertiserId = useMemo(() => {
    const map = new Map();
    storeList.forEach((store) => {
      const id = getStoreId(store);
      const advertiserId = getStoreAdvertiserId(store);
      if (id && advertiserId && !map.has(id)) {
        map.set(id, advertiserId);
      }
    });
    advertiserToStores.forEach((stores, advertiserId) => {
      stores.forEach((storeId) => {
        if (storeId && !map.has(storeId)) {
          map.set(storeId, advertiserId);
        }
      });
    });
    return map;
  }, [advertiserToStores, storeList]);

  const storeToBusinessCenter = useMemo(() => {
    const map = new Map();
    storeList.forEach((store) => {
      const id = getStoreId(store);
      if (!id || map.has(id)) return;
      const candidates = collectStoreBusinessCenterCandidates(store);
      if (candidates.length > 0) {
        map.set(id, candidates[0]);
        return;
      }
      const advertiserId = storeToAdvertiserId.get(id);
      const bcId = advertiserId ? advertiserToBusinessCenter.get(advertiserId) : '';
      if (bcId) {
        map.set(id, bcId);
      }
    });
    advertiserToStores.forEach((stores, advertiserId) => {
      const bcId = advertiserToBusinessCenter.get(advertiserId);
      if (!bcId) return;
      stores.forEach((storeId) => {
        if (storeId && !map.has(storeId)) {
          map.set(storeId, bcId);
        }
      });
    });
    return map;
  }, [advertiserToBusinessCenter, advertiserToStores, storeList, storeToAdvertiserId]);

  const resolvedAdvertiserId = useMemo(
    () => advertiserId || storeToAdvertiserId.get(storeId) || savedAdvertiserId || '',
    [advertiserId, savedAdvertiserId, storeId, storeToAdvertiserId],
  );

  const advertiserTimezoneFromOptions = useMemo(() => {
    if (!resolvedAdvertiserId) return '';
    const advertiser = advertiserById.get(String(resolvedAdvertiserId));
    if (!advertiser) return '';
    return (
      advertiser.display_timezone ||
      advertiser.displayTimezone ||
      advertiser.timezone ||
      advertiser.time_zone ||
      advertiser.timeZone ||
      ''
    );
  }, [advertiserById, resolvedAdvertiserId]);
  const hasAdvertiserTimezone = Boolean(advertiserTimezoneFromOptions);

  useEffect(() => {
    setAdvertiserTimezone(timezoneUtils.resolveTimezoneLabel(advertiserTimezoneFromOptions));
  }, [advertiserTimezoneFromOptions]);

  const storeOptions = useMemo(() => {
    if (aggregatedStoreOptions.length > 0) {
      return aggregatedStoreOptions;
    }
    if (!authId) return [];
    const seen = new Set();
    return storeList
      .map((store) => ({ value: getStoreId(store), label: getStoreLabel(store), data: store }))
      .filter((option) => {
        if (!option.value) return false;
        if (seen.has(option.value)) return false;
        seen.add(option.value);
        return true;
      });
  }, [aggregatedStoreOptions, authId, storeList]);

  useEffect(() => {
    if (authId || !storeId) return;
    const matched = storeOptions.find((option) => option.value === storeId);
    if (matched?.authId) {
      setScope((prev) => ({
        ...prev,
        accountAuthId: matched.authId,
        advertiserId: matched.advertiserId || prev.advertiserId,
        bcId: matched.bcId || prev.bcId,
      }));
    }
  }, [authId, storeId, storeOptions]);

  useEffect(() => {
    if (!authId || !storeId || !scopeOptionsReady) return;
    const derivedAdvertiserId = advertiserId || storeToAdvertiserId.get(storeId) || '';
    const derivedBusinessCenterId =
      storeToBusinessCenter.get(storeId) ||
      (derivedAdvertiserId ? advertiserToBusinessCenter.get(derivedAdvertiserId) : '');
    if (!derivedAdvertiserId && !derivedBusinessCenterId) return;
    setScope((prev) => {
      const nextAdvertiserId = derivedAdvertiserId || prev.advertiserId;
      const nextBusinessCenterId = derivedBusinessCenterId || prev.bcId;
      if (nextAdvertiserId === prev.advertiserId && nextBusinessCenterId === prev.bcId) {
        return prev;
      }
      return {
        ...prev,
        advertiserId: nextAdvertiserId || null,
        bcId: nextBusinessCenterId || null,
      };
    });
  }, [
    advertiserToBusinessCenter,
    authId,
    scopeOptionsReady,
    storeId,
    storeToAdvertiserId,
    storeToBusinessCenter,
  ]);

  const autoBindingVerified = Boolean(bindingReady || bindingConfigMatchedScope === true);

  const campaignsQueryEnabled = shouldFetchGmvMaxSeries({
    workspaceId,
    provider,
    authId,
    isScopeReady,
    autoBindingVerified,
    bindingConfigPending,
  });

  const campaignsBlockedMessage = useMemo(() => {
    if (!isScopeReady || campaignsQueryEnabled) return '';
    if (bindingConfigPending) {
      return '绑定配置加载中…';
    }
    if (!autoBindingVerified) {
      return '正在等待自动绑定验证完成后再加载 GMV Max 系列…';
    }
    return '';
  }, [
    bindingConfigPending,
    campaignsQueryEnabled,
    autoBindingVerified,
    isScopeReady,
  ]);

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

  const automationStatsRangeParams = useMemo(
    () => computeOverviewRange(automationStatsRangeKey, automationStatsCustomRange, advertiserTimezone),
    [advertiserTimezone, automationStatsCustomRange, automationStatsRangeKey],
  );
  const automationStatsEffectiveRangeParams = useMemo(
    () => {
      if (automationStatsRangeParams.start_date && automationStatsRangeParams.end_date) {
        return automationStatsRangeParams;
      }
      return computeOverviewRange('today', { start: '', end: '' }, advertiserTimezone);
    },
    [advertiserTimezone, automationStatsRangeParams],
  );

  const automationStatsRangeLabel = useMemo(
    () => (
      automationStatsRangeParams.start_date && automationStatsRangeParams.end_date
        ? formatOverviewRangeLabel(
          automationStatsRangeKey,
          automationStatsRangeParams,
          advertiserTimezone,
          hasAdvertiserTimezone,
        )
        : '自定义日期未完整填写，暂按今日显示'
    ),
    [
      advertiserTimezone,
      automationStatsRangeKey,
      automationStatsRangeParams,
      hasAdvertiserTimezone,
    ],
  );

  const handleAutomationStatsRangeChange = useCallback((rangeKey) => {
    setAutomationStatsRangeKey(rangeKey);
    if (rangeKey !== 'custom') {
      setAutomationStatsCustomRange({ start: '', end: '' });
    }
  }, []);

  const handleAutomationStatsCustomRangeChange = useCallback((key, value) => {
    setAutomationStatsCustomRange((prev) => ({ ...prev, [key]: value }));
    setAutomationStatsRangeKey('custom');
  }, []);

  const productParams = useMemo(
    () => ({
      store_id: storeId || undefined,
      advertiser_id: advertiserId || undefined,
      owner_bc_id: businessCenterId || undefined,
      automation_stats_start_date: automationStatsEffectiveRangeParams.start_date || undefined,
      automation_stats_end_date: automationStatsEffectiveRangeParams.end_date || undefined,
      page_size: 500,
    }),
    [
      advertiserId,
      automationStatsEffectiveRangeParams.end_date,
      automationStatsEffectiveRangeParams.start_date,
      businessCenterId,
      storeId,
    ],
  );

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    productParams,
    {
      enabled: Boolean(workspaceId && provider && isScopeReady),
      staleTime: 30 * 1000,
      refetchInterval: isScopeReady ? Math.min(autoRefreshMs || 60 * 1000, 60 * 1000) : undefined,
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
    },
  );

  const campaignParams = useMemo(() => {
    const params = { page_size: 50 };
    const performanceRange = computeOverviewRange(
      overviewRangeKey,
      overviewCustomRange,
      advertiserTimezone,
    );
    if (businessCenterId) params.owner_bc_id = businessCenterId;
    if (advertiserId) params.advertiser_id = advertiserId;
    if (storeId) params.store_ids = [String(storeId)];
    if (performanceRange.start_date) params.performance_start_date = performanceRange.start_date;
    if (performanceRange.end_date) params.performance_end_date = performanceRange.end_date;
    if (seriesStatusFilter === 'running') params.primary_status = 'ENABLE';
    if (seriesStatusFilter === 'paused') params.primary_status = 'DISABLE';
    if (includeDeletedCampaigns) params.include_deleted = 1;
    return params;
  }, [
    advertiserId,
    advertiserTimezone,
    businessCenterId,
    includeDeletedCampaigns,
    overviewCustomRange,
    overviewRangeKey,
    seriesStatusFilter,
    storeId,
  ]);

  const campaignsQuery = useGmvMaxCampaignsQuery(
    workspaceId,
    provider,
    authId,
    campaignParams,
    {
      enabled: campaignsQueryEnabled,
      staleTime: 15 * 1000,
      refetchInterval: campaignsQueryEnabled ? Math.min(autoRefreshMs, 60 * 1000) : undefined,
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
    },
  );

  const hermesDailyReportParams = useMemo(
    () => ({
      advertiser_id: advertiserId || undefined,
      store_id: storeId || undefined,
      page_size: 60,
      fetch_all_pages: true,
    }),
    [advertiserId, storeId],
  );

  const hermesDailyReportsQuery = useGmvMaxHermesDailyReportsQuery(
    workspaceId,
    provider,
    authId,
    hermesDailyReportParams,
    {
      enabled: Boolean(workspaceId && provider && authId && advertiserId && storeId),
      refetchInterval: autoRefreshMs,
    },
  );

  const hermesDailyReports = useMemo(
    () => normalizeReportList(hermesDailyReportsQuery.data),
    [hermesDailyReportsQuery.data],
  );

  const selectedDailyReport = useMemo(() => {
    if (hermesDailyReports.length === 0) return null;
    if (!selectedDailyReportDate) return hermesDailyReports[0];
    return hermesDailyReports.find((report) => report.report_date === selectedDailyReportDate)
      || hermesDailyReports[0];
  }, [hermesDailyReports, selectedDailyReportDate]);

  const selectedDailyReportRecommendations = useMemo(
    () => getReportRecommendationItems(selectedDailyReport),
    [selectedDailyReport],
  );
  const selectedDailyPlanDefaults = useMemo(
    () => (Array.isArray(selectedDailyReport?.plan_defaults) ? selectedDailyReport.plan_defaults : []),
    [selectedDailyReport],
  );

  useEffect(() => {
    if (hermesDailyReports.length === 0) {
      setSelectedDailyReportDate('');
      return;
    }
    const latestDate = hermesDailyReports[0]?.report_date || '';
    setSelectedDailyReportDate((current) => (
      current && hermesDailyReports.some((report) => report.report_date === current)
        ? current
        : latestDate
    ));
  }, [hermesDailyReports]);

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

  const scopeStatus = useMemo(() => {
    if (!storeId) {
      return {
        variant: 'muted',
        message: '请选择店铺以配置 GMV Max 绑定。',
      };
    }
    if (bindingConfigPending) {
      return { variant: 'muted', message: '绑定配置加载中…' };
    }
    if (bindingConfigError) {
      return {
        variant: 'error',
        message: `绑定配置加载失败：${formatError(bindingConfigError)}`,
      };
    }
    if (bindingConfig && savedStoreId && savedStoreId !== storeId) {
      const parts = [
        savedStoreId ? `store ${savedStoreId}` : '',
        savedBusinessCenterId ? `BC ${savedBusinessCenterId}` : '',
        savedAdvertiserId ? `advertiser ${savedAdvertiserId}` : '',
      ].filter(Boolean);
      const savedScope = parts.length ? parts.join(' / ') : '其他范围';
      return {
        variant: 'warning',
        message: `检测到 ${savedScope} 的已保存绑定。请选择对应店铺或为当前范围重新执行自动绑定。`,
      };
    }
    if (bindingStatusQuery.isLoading || bindingStatusQuery.isFetching) {
      return { variant: 'muted', message: '绑定状态检查中…' };
    }
    if (bindingStatus && !bindingStatus.binding_ready) {
      return {
        variant: 'error',
        message: bindingStatus.error_message || 'GMV Max 绑定未就绪，请重试。',
      };
    }
    if (!autoBindingVerified) {
      return {
        variant: 'warning',
        message: '正在等待绑定完成后再同步 GMV Max。',
      };
    }
    return { variant: 'success', message: '店铺与广告主绑定正常' };
  }, [
    bindingConfigError,
    bindingConfigPending,
    bindingStatus,
    bindingStatusQuery.isFetching,
    bindingStatusQuery.isLoading,
    savedAdvertiserId,
    savedBusinessCenterId,
    savedStoreId,
    autoBindingVerified,
    storeId,
  ]);
  const scopeStatusClassName = `gmvmax-status-banner gmvmax-status-banner--${scopeStatus.variant || 'muted'}`;

  const updateSyncIntervalMutation = useUpdateGmvMaxSyncIntervalMutation(workspaceId, provider, authId);
  const rebindAutoMutation = useGmvMaxRebindAutoMutation(workspaceId, provider, authId);

  const handleAutoRefreshChange = useCallback(
    (event) => {
      const value = Number(event?.target?.value || DEFAULT_AUTO_REFRESH_INTERVAL);
      const allowedOptions = refreshIntervalOptions.length > 0 ? refreshIntervalOptions : AUTO_REFRESH_OPTIONS;
      const normalized = allowedOptions.includes(value)
        ? value
        : allowedOptions[0] || DEFAULT_AUTO_REFRESH_INTERVAL;
      setIntervalNotice(null);
      setAutoRefreshInterval(normalized);
      if (!workspaceId || !authId) return;

      updateSyncIntervalMutation.mutate(
        { interval: normalized },
        {
          onSuccess: (data) => {
            const nextInterval =
              data?.interval && allowedOptions.includes(Number(data.interval))
                ? Number(data.interval)
                : normalized;
            setAutoRefreshInterval(nextInterval);
            queryClient.setQueryData(
              ['gmvMax', 'sync-interval', workspaceId, provider, authId],
              (prev) => ({ ...(prev || {}), interval: nextInterval }),
            );
            setIntervalNotice({
              variant: 'success',
              message: data?.message || '同步间隔已更新，将在下一轮生效。',
            });
          },
          onError: (error) => {
            setIntervalNotice({ variant: 'error', message: formatError(error) });
          },
        },
      );
    },
    [
      authId,
      provider,
      queryClient,
      refreshIntervalOptions,
      updateSyncIntervalMutation,
      workspaceId,
    ],
  );

  const handleRebindBinding = useCallback(async () => {
    if (!workspaceId || !provider || !authId || !storeId) return;
    setSyncError(null);
    try {
      await rebindAutoMutation.mutateAsync({ store_id: storeId });
      await Promise.all([bindingStatusQuery.refetch(), bindingConfigQuery.refetch()]);
      setSyncNotice({ variant: 'success', message: '已重新尝试绑定，请稍后刷新绑定状态。' });
    } catch (error) {
      setSyncError(formatError(error));
    }
  }, [
    authId,
    bindingConfigQuery,
    bindingStatusQuery,
    provider,
    rebindAutoMutation,
    storeId,
    workspaceId,
  ]);

  const handleAccountChange = useCallback((event) => {
    const value = event?.target?.value || '';
    setScope({
      accountAuthId: value ? String(value) : null,
      bcId: null,
      advertiserId: null,
      storeId: null,
    });
  }, []);

  const handleStoreChange = useCallback((event) => {
    const value = event?.target?.value || '';
    const matched = storeOptions.find((option) => option.value === value);
    setScope((prev) => ({
      ...prev,
      accountAuthId: matched?.authId || prev.accountAuthId,
      advertiserId: matched?.advertiserId ? String(matched.advertiserId) : null,
      bcId: matched?.bcId ? String(matched.bcId) : null,
      storeId: value ? String(value) : null,
    }));
  }, [storeOptions]);

  useEffect(() => {
    if (selectedProductIds.size > 0) {
      setSelectedProductIds(new Set());
    }
  }, [advertiserId, authId, businessCenterId, selectedProductIds, storeId, workspaceId]);

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
  }, [advertiserId, advertiserOptions, businessCenterId, scopeOptionsReady]);

  useEffect(() => {
    if (!storeId || !scopeOptionsReady) return;
    const hasStore = storeOptions.some((option) => option.value === storeId);
    if (hasStore) return;
    setScope((prev) => ({
      ...prev,
      storeId: null,
    }));
  }, [scopeOptionsReady, storeId, storeOptions]);

  useEffect(() => {
    if (!isScopeReady && selectedProductIds.size > 0) {
      setSelectedProductIds(new Set());
    }
  }, [isScopeReady, selectedProductIds]);

  useEffect(() => {
    setSyncError(null);
  }, [advertiserId, authId, businessCenterId, storeId]);

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

  const resolveStoreName = useCallback(
    (campaign) => {
      const candidateId =
        campaign?.store_id || campaign?.storeId || campaign?.campaign_store_id || campaign?.store?.id || null;
      if (candidateId && storeNameById.has(String(candidateId))) {
        return storeNameById.get(String(candidateId));
      }
      if (storeId && storeNameById.has(String(storeId))) {
        return storeNameById.get(String(storeId));
      }
      return (
        campaign?.store_name || campaign?.storeName || campaign?.store_label || campaign?.storeLabel || ''
      );
    },
    [storeId, storeNameById],
  );

  const products = useMemo(() => {
    if (!isScopeReady) return [];
    const data = productsQuery.data;
    const items = data?.items || data?.list || data || [];
    return Array.isArray(items) ? items : [];
  }, [isScopeReady, productsQuery.data]);

  const campaigns = useMemo(() => {
    if (!campaignsQueryEnabled) return [];
    const data = campaignsQuery.data;
    const items = data?.items || data?.list || data || [];
    return filterCampaignsByStatus(Array.isArray(items) ? items : [], {
      includeDeleted: includeDeletedCampaigns,
    });
  }, [campaignsQuery.data, campaignsQueryEnabled, includeDeletedCampaigns]);

  const overviewRangeParams = useMemo(
    () => ({
      ...computeOverviewRange(overviewRangeKey, overviewCustomRange, advertiserTimezone),
      store_id: storeId ? String(storeId) : undefined,
    }),
    [advertiserTimezone, overviewCustomRange, overviewRangeKey, storeId],
  );
  const overviewRangeLabel = useMemo(
    () => formatOverviewRangeLabel(
      overviewRangeKey,
      overviewRangeParams,
      advertiserTimezone,
      hasAdvertiserTimezone,
    ),
    [advertiserTimezone, hasAdvertiserTimezone, overviewRangeKey, overviewRangeParams],
  );
  const hasValidOverviewRange = useMemo(
    () => Boolean(overviewRangeParams.start_date && overviewRangeParams.end_date),
    [overviewRangeParams.end_date, overviewRangeParams.start_date],
  );

  const handleOverviewRangeChange = useCallback((rangeKey) => {
    setOverviewRangeKey(rangeKey);
    if (rangeKey !== 'custom') {
      setOverviewCustomRange({ start: '', end: '' });
    }
  }, []);

  const handleOverviewCustomRangeChange = useCallback((key, value) => {
    setOverviewCustomRange((prev) => ({ ...prev, [key]: value }));
    setOverviewRangeKey('custom');
  }, []);

  const syncTask = useGmvSyncTask({ workspaceId, provider, authId });
  const isSyncInProgress = isSyncing || syncTask.isSyncing;
  const shouldFetchMetrics = useMemo(
    () =>
      Boolean(
        workspaceId &&
          authId &&
          storeId &&
          campaignsQueryEnabled &&
          !isSyncInProgress &&
          hasValidOverviewRange,
      ),
    [authId, campaignsQueryEnabled, hasValidOverviewRange, isSyncInProgress, storeId, workspaceId],
  );

  const overviewSummaryQuery = useGmvMaxMetricsQuery(
    workspaceId,
    provider,
    authId,
    undefined,
    {
      start_date: overviewRangeParams.start_date,
      end_date: overviewRangeParams.end_date,
      store_id: overviewRangeParams.store_id,
      level: GmvMaxMetricsLevel.OVERVIEW,
    },
    {
      enabled: shouldFetchMetrics,
      staleTime: 60 * 1000,
      refetchInterval: shouldFetchMetrics ? autoRefreshMs : undefined,
      keepPreviousData: true,
    },
  );
  const overviewSummary = useMemo(
    () => deriveOverviewSnapshotSummary(overviewSummaryQuery.data),
    [overviewSummaryQuery.data],
  );
  const overviewFreshness = useMemo(
    () => formatDataFreshness(overviewSummaryQuery.data?.freshness),
    [overviewSummaryQuery.data?.freshness],
  );
  const overviewCostPerOrder = useMemo(() => {
    if (!overviewSummary) return null;
    if (overviewSummary.costPerOrder !== undefined && overviewSummary.costPerOrder !== null) {
      return overviewSummary.costPerOrder;
    }
    if (!overviewSummary.orders) return null;
    return overviewSummary.orders > 0 ? overviewSummary.spend / overviewSummary.orders : null;
  }, [overviewSummary]);

  const refreshMetrics = useCallback(async () => {
    if (typeof overviewSummaryQuery.refetch === 'function') {
      await overviewSummaryQuery.refetch();
    }
  }, [overviewSummaryQuery.refetch]);

  const campaignCards = useMemo(
    () =>
      campaigns.map((campaign) => ({
        campaign,
        detail: null,
        strategy: null,
        detailLoading: false,
        strategyLoading: false,
        scopeFallback: campaignScopeSnapshot,
      })),
    [campaignScopeSnapshot, campaigns],
  );

  const filteredCampaignCards = useMemo(() => {
    if (!campaignsQueryEnabled) return [];
    return campaignCards.filter((card) => {
      const { matches, pending } = matchesCampaignScope(card, {
        businessCenterId,
        advertiserId,
        storeId,
      });
      return matches && !pending && !isCampaignDeleted(card.campaign);
    });
  }, [
    advertiserId,
    businessCenterId,
    campaignCards,
    campaignsQueryEnabled,
    includeDeletedCampaigns,
    storeId,
  ]);

  const campaignCardsWithMeta = useMemo(() => {
    return filteredCampaignCards.map((card) => {
      const deleted = isCampaignDeleted(card.campaign);
      const statusMeta = getCampaignStatusMeta(
        card.campaign?.operation_status ||
          card.campaign?.status ||
          card.detail?.campaign?.operation_status ||
          card.detail?.campaign?.status,
        {
          isDeleted: deleted,
          deletedLabel: GmvMaxTexts.statusDeleted || '已删除',
        },
      );
      const createdAt =
        Date.parse(card.campaign?.created_time || card.campaign?.create_time || card.campaign?.createdAt || '') ||
        parseFloat(card.campaign?.created_time || card.campaign?.create_time || card.campaign?.createdAt || '0') ||
        0;
      return {
        ...card,
        isDeleted: deleted,
        statusMeta,
        createdAt,
        storeName: resolveStoreName(card.campaign),
      };
    });
  }, [filteredCampaignCards, resolveStoreName]);

  const seriesRows = useMemo(() => {
    const search = seriesSearch.trim().toLowerCase();
    return campaignCardsWithMeta
      .map((card) => {
        return {
          ...card,
          storeId: getStoreId(card.campaign) || card.campaign?.store_id || card.campaign?.storeId,
        };
      })
      .filter((card) => {
        if (seriesStatusFilter !== 'all' && card.statusMeta?.category !== seriesStatusFilter) return false;
        if (seriesStoreFilter && card.storeId && String(card.storeId) !== String(seriesStoreFilter)) return false;
        if (search) {
          const name =
            card.campaign?.name || card.campaign?.campaign_name || card.detail?.campaign?.name || '';
          if (!String(name).toLowerCase().includes(search)) return false;
        }
        return true;
      });
  }, [campaignCardsWithMeta, seriesSearch, seriesStatusFilter, seriesStoreFilter]);

  const sortedSeriesRows = useMemo(() => {
    const list = [...seriesRows];
    const getCreated = (card) => (Number.isFinite(card.createdAt) ? card.createdAt : 0);
    const getPerformance = (card, key) => {
      const performance = card.campaign?.performance || card.detail?.campaign?.performance || {};
      const value = key === 'spend'
        ? performance.cost ?? performance.spend
        : performance.gmv ?? performance.gross_revenue;
      const number = Number(value);
      return Number.isFinite(number) ? number : 0;
    };
    const getName = (card) => String(
      card.campaign?.name || card.campaign?.campaign_name || card.detail?.campaign?.name || '',
    );
    list.sort((a, b) => {
      if (sortOption === 'spend') return getPerformance(b, 'spend') - getPerformance(a, 'spend');
      if (sortOption === 'gmv') return getPerformance(b, 'gmv') - getPerformance(a, 'gmv');
      if (sortOption === 'name') return getName(a).localeCompare(getName(b), 'zh-CN');
      return getCreated(b) - getCreated(a);
    });
    return list;
  }, [seriesRows, sortOption]);

  const deletedCampaignCards = useMemo(() => {
    if (!includeDeletedCampaigns || !campaignsQueryEnabled) return [];
    return campaignCards.filter((card) => {
      const { matches, pending } = matchesCampaignScope(card, {
        businessCenterId,
        advertiserId,
        storeId,
      });
      return matches && !pending && isCampaignDeleted(card.campaign);
    });
  }, [
    advertiserId,
    businessCenterId,
    campaignCards,
    campaignsQueryEnabled,
    includeDeletedCampaigns,
    storeId,
  ]);

  const metadataSyncMutation = useSyncAccountMetadataMutation(workspaceId, provider, authId);
  const productSyncMutation = useSyncAccountProductsMutation(workspaceId, provider, authId);
  const balanceSyncMutation = useSyncAdvertiserBalanceMutation(workspaceId, provider, authId);
  const balanceSyncMutateRef = useRef(balanceSyncMutation.mutate);
  const syncStatusLabel = useMemo(() => {
    const state = (syncTask.lastState || '').toUpperCase();
    if (state === 'PENDING') return '排队中…';
    if (state === 'STARTED' || state === 'RETRY') return '同步中…';
    if (state === 'SUCCESS') return '同步完成';
    if (state === 'FAILURE' || state === 'REVOKED') return '同步失败';
    if (state === 'TIMEOUT') return '同步超时';
    return isSyncInProgress ? '同步中…' : '';
  }, [isSyncInProgress, syncTask.lastState]);

  useEffect(() => {
    if (!syncTask.error) return;
    const message = formatError(syncTask.error) || '同步失败，请稍后再试。';
    setSyncError(message);
    setSyncNotice(null);
  }, [syncTask.error]);

  const refreshScopeQueries = useCallback(() => {
    if (!workspaceId || !provider || !authId) {
      return Promise.resolve();
    }
    const invalidateCampaigns = queryClient.invalidateQueries({
      queryKey: ['gmvMax', 'campaigns', workspaceId, provider, authId],
    });
    const invalidateProducts = queryClient.invalidateQueries({
      queryKey: ['gmvMax', 'products', workspaceId, provider, authId],
    });
    return Promise.all([invalidateCampaigns, invalidateProducts]);
  }, [authId, provider, queryClient, workspaceId]);
  const canCreateSeries = Boolean(isScopeReady);

  useEffect(() => {
    if (!syncTask.isSyncing) return;
    setSyncNotice((prev) =>
      prev?.variant === 'info'
        ? prev
        : { variant: 'info', message: '正在同步 GMV Max 数据，请稍候…' },
    );
  }, [syncTask.isSyncing]);

  useEffect(() => {
    if (!isSyncInProgress && syncTask.lastState === 'SUCCESS') {
      setSyncNotice({ variant: 'success', message: '同步完成，数据已刷新。' });
      setSyncError(null);
    }
  }, [isSyncInProgress, syncTask.lastState]);

  useEffect(() => {
    if (syncTask.lastState !== 'SUCCESS' || isSyncInProgress) return;
    refreshMetrics();
  }, [isSyncInProgress, refreshMetrics, syncTask.lastState]);

  const performCampaignSync = useCallback(async () => {
    const result = await syncTask.startSync({
      start_date: overviewRangeParams.start_date || null,
      end_date: overviewRangeParams.end_date || null,
      levels: ['OVERVIEW'],
      campaign_ids: null,
    });
    const completed = result?.completion ? await result.completion : result;
    const completedState = String(completed?.state || result?.state || '').toUpperCase();
    if (completedState === 'SUCCESS') {
      await refreshScopeQueries();
      return 'SUCCESS';
    }
    throw new Error('同步失败，请稍后再试。');
  }, [overviewRangeParams.end_date, overviewRangeParams.start_date, refreshScopeQueries, syncTask]);

  const performSync = useCallback(async () => {
    if (!workspaceId || !provider || !authId) return;
    let nextNotice = null;
    let nextError = null;

    if (!isScopeReady) {
      setSyncNotice(null);
      setSyncError('请先选择店铺以完成数据同步。');
      return;
    }
    if (bindingConfigPending) {
      setSyncNotice(null);
      setSyncError('绑定配置加载中，请稍后再试。');
      return;
    }
    if (!bindingReady) {
      setSyncNotice(null);
      setSyncError('请先完成店铺-广告主绑定后再同步 GMV Max 数据。');
      return;
    }
    if (!hasValidOverviewRange) {
      setSyncNotice(null);
      setSyncError('请选择有效的时间范围后再同步 GMV Max 数据。');
      return;
    }
    if (syncInFlightRef.current || isSyncing || syncTask.isSyncing) return;

    syncInFlightRef.current = true;
    setSyncNotice({ variant: 'info', message: '正在同步 GMV Max 数据，请稍候…' });
    setSyncError(null);
    setIsSyncing(true);
    try {
      const deferredSteps = [];
      const runFoundationStep = async (label, action) => {
        try {
          await action();
        } catch (error) {
          if (isSyncRateLimitedError(error)) {
            deferredSteps.push(label);
            return;
          }
          throw error;
        }
      };

      await runFoundationStep('授权资料', () => (
        metadataSyncMutation.mutateAsync({ scope: 'meta', mode: 'full' })
      ));
      const refetchPromises = [];
      if (typeof accountsQuery.refetch === 'function') {
        refetchPromises.push(accountsQuery.refetch());
      }
      if (typeof scopeOptionsQuery.refetch === 'function') {
        refetchPromises.push(scopeOptionsQuery.refetch());
      }
      if (refetchPromises.length > 0) {
        await Promise.all(refetchPromises);
      }
      queryClient.invalidateQueries({ queryKey: scopeOptionsQueryKey });
      queryClient.invalidateQueries({ queryKey: accountsQueryKey });

      const bcForSync = savedBusinessCenterId || (businessCenterId ? String(businessCenterId) : '');
      const advertiserForSync = savedAdvertiserId || (advertiserId ? String(advertiserId) : '');
      if (bcForSync && advertiserForSync && storeId) {
        await runFoundationStep('余额', () => balanceSyncMutation.mutateAsync({
          bc_id: bcForSync,
          advertiser_id: advertiserForSync,
          store_id: storeId,
        }));
      }

      await runFoundationStep('商品资料', () => productSyncMutation.mutateAsync({
        scope: 'products',
        mode: 'full',
        bc_id: businessCenterId ? String(businessCenterId) : undefined,
        advertiser_id: advertiserId ? String(advertiserId) : undefined,
        store_id: storeId ? String(storeId) : undefined,
        product_eligibility: 'gmv_max',
      }));

      let finalState = null;
      try {
        finalState = await performCampaignSync();
      } catch (error) {
        if (!isSyncRateLimitedError(error)) throw error;
        await Promise.all([refreshScopeQueries(), refreshMetrics()]);
        nextNotice = {
          variant: 'info',
          message: '后台刚完成或正在执行同步，已刷新当前最新入库数据。',
        };
      }
      if (finalState === 'SUCCESS') {
        await refreshMetrics();
        nextNotice = {
          variant: 'success',
          message: deferredSteps.length > 0
            ? `投放数据已刷新；${deferredSteps.join('、')}仍在同步冷却期，将由后台任务继续更新。`
            : '同步完成，数据已刷新。',
        };
      }
    } catch (error) {
      console.error('Failed to sync GMV Max data automatically', error);
      const message = formatError(error);
      const normalized = message || '同步失败，请稍后再试。';
      nextError = normalized.trim().startsWith('[') ? '同步失败，请稍后再试。' : normalized;
    } finally {
      syncInFlightRef.current = false;
      setIsSyncing(false);
      if (nextError) {
        setSyncError(nextError);
        setSyncNotice(null);
      } else if (nextNotice) {
        setSyncNotice(nextNotice);
        setSyncError(null);
      }
    }
  }, [
    accountsQuery,
    accountsQueryKey,
    advertiserId,
    authId,
    autoBindingVerified,
    balanceSyncMutation,
    bindingConfigPending,
    bindingReady,
    businessCenterId,
    hasValidOverviewRange,
    isScopeReady,
    isSyncing,
    metadataSyncMutation,
    performCampaignSync,
    productSyncMutation,
    provider,
    queryClient,
    refreshMetrics,
    savedAdvertiserId,
    savedBusinessCenterId,
    scopeOptionsQuery,
    scopeOptionsQueryKey,
    syncTask,
    storeId,
    workspaceId,
  ]);

  useEffect(() => {
    balanceSyncMutateRef.current = balanceSyncMutation.mutate;
  }, [balanceSyncMutation.mutate]);

  useEffect(() => {
    if (balanceRefreshIntervalRef.current) {
      clearInterval(balanceRefreshIntervalRef.current);
      balanceRefreshIntervalRef.current = null;
    }
    if (!workspaceId || !provider || !authId || !businessCenterId || !advertiserId || !storeId) {
      return undefined;
    }

    const syncBalance = () => {
      balanceSyncMutateRef.current?.({
        bc_id: businessCenterId,
        advertiser_id: advertiserId,
        store_id: storeId,
      });
    };

    syncBalance();
    balanceRefreshIntervalRef.current = setInterval(syncBalance, BALANCE_REFRESH_INTERVAL_MS);
    return () => {
      if (balanceRefreshIntervalRef.current) {
        clearInterval(balanceRefreshIntervalRef.current);
        balanceRefreshIntervalRef.current = null;
      }
    };
  }, [advertiserId, authId, businessCenterId, provider, storeId, workspaceId]);

  const handleOpenCreate = useCallback(() => {
    if (!canCreateSeries) return;
    setCreateModalOpen(true);
  }, [canCreateSeries]);

  const handleCloseCreate = useCallback(() => {
    setCreateModalOpen(false);
  }, []);

  const handleSeriesCreated = useCallback(() => {
    setCreateModalOpen(false);
    refreshScopeQueries();
  }, [
    authId,
    provider,
    refreshScopeQueries,
    workspaceId,
  ]);

  function handleEditRequest(campaignId) {
    setEditingCampaignId(String(campaignId));
  }

  function handleCloseEdit() {
    setEditingCampaignId('');
  }

  function handleSeriesUpdated() {
    setEditingCampaignId('');
    refreshScopeQueries();
  }

  const resetPageError = useCallback(() => {
    setSyncError(null);
    setSyncNotice(null);
    refreshMetrics();
  }, [refreshMetrics]);

  const buildCampaignSearchParams = useCallback(
    (tab) => {
      const params = new URLSearchParams();
      if (tab) params.set('tab', tab);
      if (provider) params.set('provider', provider);
      if (authId) params.set('authId', authId);
      if (businessCenterId) params.set('businessCenterId', businessCenterId);
      if (advertiserId) params.set('advertiserId', advertiserId);
      if (storeId) params.set('storeId', storeId);
      if (advertiserTimezone) params.set('timezone', advertiserTimezone);
      return params.toString() ? `?${params.toString()}` : '';
    },
    [advertiserId, advertiserTimezone, authId, businessCenterId, provider, storeId],
  );

  const handleManage = useCallback(
    (campaignId) => {
      const search = buildCampaignSearchParams('products');
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

  const editingDetailResult = useGmvMaxCampaignQuery(
    workspaceId,
    provider,
    authId,
    editingCampaignId,
    {
      enabled: Boolean(workspaceId && provider && authId && editingCampaignId),
      staleTime: 60 * 1000,
    },
  );

  const editingCampaign = useMemo(
    () => campaigns.find((item) => String(item?.campaign_id ?? item?.id) === String(editingCampaignId)) || null,
    [campaigns, editingCampaignId],
  );

  const editingDetail = editingDetailResult?.data;
  const editingDetailLoading = editingDetailResult?.isLoading ?? false;
  const editingDetailError = editingDetailResult?.error;
  const editingDetailRefetch = editingDetailResult?.refetch;

  const campaignsLoading = Boolean(campaignsQueryEnabled && campaignsQuery.isLoading);
  const campaignsRefreshing = Boolean(
    campaignsQueryEnabled && campaignsQuery.isFetching && !campaignsQuery.isLoading,
  );
  const productsLoading = Boolean(isScopeReady && productsQuery.isLoading);
  const productsRefreshing = Boolean(
    isScopeReady && productsQuery.isFetching && !productsQuery.isLoading,
  );
  const balanceTimestamp = advertiserBalance?.fetched_at;
  const canDisplayBalance = Boolean(storeId && bindingConfigMatchedScope);
  const isSyncIntervalUpdating = Boolean(
    updateSyncIntervalMutation?.isPending || updateSyncIntervalMutation?.isLoading,
  );

  return (
    <GmvMaxPageErrorBoundary onReset={resetPageError}>
      <div className="gmvmax-page">
      {isSyncInProgress ? (
        <div className="gmvmax-status-banner gmvmax-status-banner--muted">
          {syncStatusLabel || '数据同步中…'}
        </div>
      ) : null}
      {bindingErrorMessage ? (
        <div className="gmvmax-status-banner gmvmax-status-banner--error gmvmax-status-banner--actions">
          <span>{`同步失败：${bindingErrorMessage}`}</span>
          <button
            type="button"
            className="gmvmax-button"
            onClick={handleRebindBinding}
            disabled={rebindAutoMutation.isPending || bindingStatusRefreshing}
          >
            {rebindAutoMutation.isPending ? '重试中…' : '重试绑定'}
          </button>
        </div>
      ) : null}
      {syncError ? (
        <div className="gmvmax-status-banner gmvmax-status-banner--error">
          同步失败：{syncError}
        </div>
      ) : null}
      {syncNotice ? (
        <div className={`gmvmax-status-banner gmvmax-status-banner--${syncNotice.variant || 'muted'}`}>
          {syncNotice.message}
        </div>
      ) : null}
      {intervalNotice ? (
        <div
          className={`gmvmax-status-banner gmvmax-status-banner--${intervalNotice.variant || 'muted'}`}
        >
          {intervalNotice.message}
        </div>
      ) : null}
      <header className="gmvmax-overview-header">
        <div className="gmvmax-overview-header__title">
          <h1>{GmvMaxTexts.overviewTitle}</h1>
          <p className="gmvmax-page__subtitle">{GmvMaxTexts.overviewSubtitle}</p>
        </div>
        <div className="gmvmax-overview-header__actions">
          <span className="gmvmax-provider-badge">{`${GmvMaxTexts.providerLabel}：${PROVIDER_LABEL}`}</span>
          <button
            type="button"
            className="gmvmax-button gmvmax-button--primary"
            onClick={performSync}
            disabled={
              isSyncInProgress ||
              !isScopeReady ||
              bindingConfigPending ||
              !bindingReady ||
              !hasValidOverviewRange
            }
            title={bindingReady ? undefined : '请先完成店铺-广告主绑定'}
          >
            {isSyncInProgress ? syncStatusLabel || '同步中…' : '同步数据'}
          </button>
        </div>
        <div className="gmvmax-balance-chip gmvmax-overview-balance">
            <div className="gmvmax-balance-chip__row">
              <div>
                <p className="gmvmax-balance-chip__title">{GmvMaxTexts.advertiserBalance}</p>
                <p className="gmvmax-balance-chip__timestamp">
                  {canDisplayBalance && balanceTimestamp
                    ? `${GmvMaxTexts.balanceUpdatedPrefix} ${formatISODate(balanceTimestamp)}`
                    : GmvMaxTexts.balanceUnavailable}
                </p>
                {advertiserTimezone ? (
                  <p className="gmvmax-balance-chip__timezone">
                    {hasAdvertiserTimezone
                      ? `按广告主时区：${advertiserTimezone}`
                      : `广告主时区未知，暂按浏览器时区：${advertiserTimezone}`}
                  </p>
                ) : null}
              </div>
            </div>
            <div className="gmvmax-balance-chip__values">
              {!storeId ? (
                <span className="gmvmax-balance-chip__placeholder">{GmvMaxTexts.selectStoreToViewBalance}</span>
              ) : !bindingConfigMatchedScope ? (
                <span className="gmvmax-balance-chip__placeholder">{GmvMaxTexts.bindingPending}</span>
              ) : advertiserBalance ? (
                <>
                  <div className="gmvmax-balance-banner__value-block">
                    <span className="gmvmax-balance-banner__label">{GmvMaxTexts.balanceCashLabel ?? '现金'}</span>
                    <span className="gmvmax-balance-banner__value">
                      {formatMoney(advertiserBalance?.cash_balance)}
                      {advertiserBalance?.currency ? (
                        <span className="gmvmax-balance-banner__currency">{advertiserBalance.currency}</span>
                      ) : null}
                    </span>
                  </div>
                  <div className="gmvmax-balance-banner__value-block">
                    <span className="gmvmax-balance-banner__label">
                      {GmvMaxTexts.balanceBudgetRemainingLabel ?? '剩余预算'}
                    </span>
                    <span className="gmvmax-balance-banner__value">
                      {advertiserBalance?.budget_remaining === null ||
                      typeof advertiserBalance?.budget_remaining === 'undefined'
                        ? '—'
                        : formatMoney(advertiserBalance.budget_remaining)}
                      {advertiserBalance?.currency &&
                      advertiserBalance?.budget_remaining !== null &&
                      typeof advertiserBalance?.budget_remaining !== 'undefined' ? (
                        <span className="gmvmax-balance-banner__currency">{advertiserBalance.currency}</span>
                      ) : null}
                    </span>
                  </div>
                </>
              ) : (
                <span className="gmvmax-balance-chip__placeholder">{GmvMaxTexts.awaitingBalance}</span>
              )}
            </div>
          </div>
      </header>

      <section className="gmvmax-card gmvmax-overview-scope">
        <header className="gmvmax-card__header">
          <div>
            <h2>投放范围</h2>
            <p>选择店铺并管理页面数据刷新。</p>
          </div>
        </header>
        <div className="gmvmax-card__body">
          <div className="gmvmax-overview-scope__controls">
            <FormField label="店铺">
              <select
                value={storeId}
                onChange={handleStoreChange}
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
            <FormField label="页面刷新">
                <select
                  value={autoRefreshInterval}
                  onChange={handleAutoRefreshChange}
                  disabled={isSyncIntervalUpdating || syncIntervalQuery.isLoading}
                >
                  {refreshIntervalOptions.map((option) => (
                    <option key={option} value={option}>{`${option} 分钟`}</option>
                  ))}
                </select>
            </FormField>
            <div className={`${scopeStatusClassName} gmvmax-overview-scope__status`}>
              <span className="gmvmax-overview-scope__status-dot" aria-hidden="true" />
              {scopeStatus.message}
            </div>
          </div>
        </div>
      </section>

      {campaignsQueryEnabled ? (
        <section className="gmvmax-card gmvmax-card--summary gmvmax-overview-performance">
          <header className="gmvmax-card__header gmvmax-card__header--stacked">
            <div className="gmvmax-card__header-title">
              <h2>{GmvMaxTexts.summaryBarTitle}</h2>
              {overviewRangeLabel ? (
                <p className="gmvmax-card__subtitle">{overviewRangeLabel}</p>
              ) : null}
            </div>
            <div className="gmvmax-card__header-actions gmvmax-card__header-actions--wrap">
              <div className="gmvmax-date-filters">
                {OVERVIEW_RANGE_OPTIONS.map((option) => (
                  <button
                    key={option.key}
                    type="button"
                    className={`gmvmax-chip ${
                      overviewRangeKey === option.key ? 'gmvmax-chip--active' : ''
                    }`}
                    onClick={() => handleOverviewRangeChange(option.key)}
                  >
                    {option.label}
                  </button>
                ))}
                {overviewRangeKey === 'custom' ? (
                  <div className="gmvmax-date-filters__custom">
                    <input
                      type="date"
                      value={overviewCustomRange.start}
                      onChange={(event) => handleOverviewCustomRangeChange('start', event.target.value)}
                    />
                    <span>至</span>
                    <input
                      type="date"
                      value={overviewCustomRange.end}
                      onChange={(event) => handleOverviewCustomRangeChange('end', event.target.value)}
                    />
                  </div>
                ) : null}
              </div>
              {overviewSummaryQuery.isLoading ? <Loading text="整体表现加载中…" /> : null}
            </div>
          </header>
          <div className="gmvmax-card__body">
            {!hasValidOverviewRange ? (
              <p className="gmvmax-placeholder">请选择完整日期范围以查看整体投放表现。</p>
            ) : (
              <>
                <div className="gmvmax-overview-summary">
                  <div className="gmvmax-overview-summary__item">
                    <span className="gmvmax-overview-summary__label">{GmvMaxTexts.totalSpend}</span>
                    <strong className="gmvmax-overview-summary__value">
                      {overviewSummary ? formatMoney(overviewSummary.spend) : '—'}
                    </strong>
                  </div>
                  <div className="gmvmax-overview-summary__item">
                    <span className="gmvmax-overview-summary__label">{GmvMaxTexts.totalGmv}</span>
                    <strong className="gmvmax-overview-summary__value">
                      {overviewSummary ? formatMoney(overviewSummary.gmv) : '—'}
                    </strong>
                  </div>
                  <div className="gmvmax-overview-summary__item">
                    <span className="gmvmax-overview-summary__label">{GmvMaxTexts.overviewRoi}</span>
                    <strong className="gmvmax-overview-summary__value">
                      {overviewSummary && overviewSummary.roas !== null ? formatRoi(overviewSummary.roas) : '—'}
                    </strong>
                  </div>
                  <div className="gmvmax-overview-summary__item">
                    <span className="gmvmax-overview-summary__label">{GmvMaxTexts.roiOrders}</span>
                    <strong className="gmvmax-overview-summary__value">
                      {overviewSummary ? overviewSummary.orders : '—'}
                    </strong>
                  </div>
                  <div className="gmvmax-overview-summary__item">
                    <span className="gmvmax-overview-summary__label">{GmvMaxTexts.costPerOrder}</span>
                    <strong className="gmvmax-overview-summary__value">
                      {overviewCostPerOrder !== null ? formatMoney(overviewCostPerOrder) : '—'}
                    </strong>
                  </div>
                </div>
                {overviewFreshness ? (
                  <div className={`gmvmax-data-freshness gmvmax-data-freshness--${overviewFreshness.state}`}>
                    <strong>{overviewFreshness.label}</strong>
                    <span>{overviewFreshness.detail}</span>
                    <span>
                      {hasAdvertiserTimezone ? '广告主时区' : '浏览器时区'} {advertiserTimezone || 'UTC'}
                    </span>
                  </div>
                ) : null}
                {overviewSummaryQuery.error ? (
                  <p className="gmvmax-placeholder">整体表现加载失败，请稍后重试。</p>
                ) : null}
              </>
            )}
          </div>
        </section>
      ) : null}

      {isScopeReady ? (
        <section className="gmvmax-card gmvmax-hermes-report">
          <header className="gmvmax-card__header">
            <div>
              <h2>每日投放报告</h2>
              <p>Hermes 会汇总 GMV Max 消耗、ROI、商品与素材策略，形成每日复盘和调优建议。</p>
            </div>
            <div className="gmvmax-card__header-actions">
              {hermesDailyReportsQuery.isFetching ? <Loading text="报告加载中…" /> : null}
              {selectedDailyReport?.updated_at ? (
                <span className="gmvmax-muted-text">
                  更新于 {formatISODate(selectedDailyReport.updated_at)}
                </span>
              ) : null}
            </div>
          </header>
          <div className="gmvmax-card__body">
            {hermesDailyReportsQuery.error ? (
              <p className="gmvmax-placeholder">日报加载失败：{formatError(hermesDailyReportsQuery.error)}</p>
            ) : null}
            {!hermesDailyReportsQuery.isLoading && hermesDailyReports.length === 0 ? (
              <p className="gmvmax-placeholder">
                暂无日报。后台会在收集到当天投放数据后生成 Hermes 每日复盘。
              </p>
            ) : null}
            {hermesDailyReports.length > 0 ? (
              <div className="gmvmax-hermes-report__layout">
                <aside className="gmvmax-hermes-report__dates" aria-label="日报日期">
                  {hermesDailyReports.map((report) => (
                    <button
                      key={`${report.id}-${report.report_date}`}
                      type="button"
                      className={`gmvmax-hermes-report__date ${
                        selectedDailyReport?.id === report.id ? 'gmvmax-hermes-report__date--active' : ''
                      }`}
                      onClick={() => setSelectedDailyReportDate(report.report_date || '')}
                    >
                      <span>{report.report_date || '未知日期'}</span>
                      <small>{formatDailyReportStatus(report.status)}</small>
                    </button>
                  ))}
                </aside>
                <article className="gmvmax-hermes-report__content">
                  <div className="gmvmax-hermes-report__meta">
                    <span>{selectedDailyReport?.report_date || '—'}</span>
                    {selectedDailyReport?.advertiser_timezone ? (
                      <span>{`广告主时区：${selectedDailyReport.advertiser_timezone}`}</span>
                    ) : null}
                  </div>
                  {selectedDailyReportRecommendations.length > 0 ? (
                    <div className="gmvmax-hermes-report__recommendations">
                      <h3>调优建议</h3>
                      <ul>
                        {selectedDailyReportRecommendations.slice(0, 6).map((item, index) => (
                          <li key={`${selectedDailyReport?.id || 'report'}-rec-${index}`}>
                            {formatRecommendationText(item)}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                  {selectedDailyPlanDefaults.length > 0 ? (
                    <div className="gmvmax-hermes-report__recommendations">
                      <h3>Hermes 已批准的明日建计划参数</h3>
                      <ul>
                        {selectedDailyPlanDefaults.map((item, index) => {
                          const params = item?.params || {};
                          const summary = [
                            params.budget !== undefined ? `预算 ${params.budget}` : '',
                            params.roas_bid !== undefined ? `ROAS ${params.roas_bid}` : '',
                            params.min_roi !== undefined ? `最低 ROI ${params.min_roi}` : '',
                            params.monitor_interval_minutes !== undefined
                              ? `监控 ${params.monitor_interval_minutes} 分钟`
                              : '',
                            params.cooldown_minutes !== undefined ? `冷却 ${params.cooldown_minutes} 分钟` : '',
                          ].filter(Boolean).join('，');
                          return (
                            <li key={`${selectedDailyReport?.id || 'report'}-default-${index}`}>
                              {item.item_group_id ? `商品 ${item.item_group_id}` : '店铺默认'}：
                              {summary || '参数待生成'}；生效日 {item.effective_date || '—'}，
                              置信度 {item.confidence || 'low'}
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  ) : null}
                  <pre className="gmvmax-hermes-report__markdown">
                    {selectedDailyReport?.report_markdown || '该日报暂无正文。'}
                  </pre>
                </article>
              </div>
            ) : null}
          </div>
        </section>
      ) : null}

      <OrderIntelligencePanel
        workspaceId={workspaceId}
        provider={provider}
        authId={authId}
        advertiserId={advertiserId}
        storeId={storeId}
        advertiserTimezone={advertiserTimezone}
        enabled={Boolean(isScopeReady && bindingReady)}
      />

      <ProductAutomationPanel
        workspaceId={workspaceId}
        provider={provider}
        authId={authId}
        advertiserId={advertiserId}
        businessCenterId={businessCenterId}
        storeId={storeId}
        advertiserTimezone={advertiserTimezone}
        bindingConfig={bindingConfig}
        products={products}
        productsLoading={productsLoading}
        productsRefreshing={productsRefreshing}
        campaignCards={campaignCardsWithMeta}
        canOperate={Boolean(isScopeReady && bindingReady)}
        onChanged={refreshScopeQueries}
        statsRangeKey={automationStatsRangeKey}
        statsCustomRange={automationStatsCustomRange}
        statsRangeLabel={automationStatsRangeLabel}
        onStatsRangeChange={handleAutomationStatsRangeChange}
        onStatsCustomRangeChange={handleAutomationStatsCustomRangeChange}
      />

      <section className="gmvmax-card gmvmax-series-section">
        <header className="gmvmax-card__header">
          <div>
            <h2>{GmvMaxTexts.gmvMaxSeries}</h2>
            {campaignsRefreshing ? <p className="gmvmax-card__subtitle">正在更新系列表现…</p> : null}
          </div>
          <div className="gmvmax-card__header-actions gmvmax-card__header-actions--wrap">
            <button
              type="button"
              className="gmvmax-button gmvmax-button--primary"
              onClick={handleOpenCreate}
              disabled={!canCreateSeries}
            >
              新建投放
            </button>
            <div className="gmvmax-series-filters">
              <div className="gmvmax-series-filters__row">
                <label className="gmvmax-select gmvmax-select--inline">
                  <span>{GmvMaxTexts.filterByStore}</span>
                  <select
                    value={seriesStoreFilter}
                    onChange={(event) => setSeriesStoreFilter(event.target.value)}
                  >
                    <option value="">{GmvMaxTexts.allStores}</option>
                    {storeOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="gmvmax-chip-group" aria-label={GmvMaxTexts.filterByStatus}>
                  {[
                    { key: 'running', label: GmvMaxTexts.statusRunning },
                    { key: 'paused', label: GmvMaxTexts.statusPaused },
                    { key: 'ended', label: GmvMaxTexts.statusEnded },
                    { key: 'all', label: GmvMaxTexts.statusAll },
                  ].map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      className={`gmvmax-chip ${seriesStatusFilter === option.key ? 'gmvmax-chip--active' : ''}`}
                      onClick={() => setSeriesStatusFilter(option.key)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <div className="gmvmax-input gmvmax-input--inline">
                  <input
                    type="search"
                    value={seriesSearch}
                    onChange={(event) => setSeriesSearch(event.target.value)}
                    placeholder={GmvMaxTexts.seriesSearchPlaceholder}
                  />
                </div>
              </div>
              <div className="gmvmax-series-filters__row gmvmax-series-filters__row--end">
                  <label className="gmvmax-select gmvmax-select--inline">
                    <span>{GmvMaxTexts.sortBy}</span>
                    <select value={sortOption} onChange={(event) => setSortOption(event.target.value)}>
                      <option value="latest">{GmvMaxTexts.sortLatest}</option>
                      <option value="spend">消耗从高到低</option>
                      <option value="gmv">GMV 从高到低</option>
                      <option value="name">系列名称</option>
                    </select>
                  </label>
                <label className="gmvmax-checkbox gmvmax-checkbox--inline">
                  <input
                    type="checkbox"
                    checked={includeDeletedCampaigns}
                    onChange={(event) => setIncludeDeletedCampaigns(event.target.checked)}
                  />
                  <span>{GmvMaxTexts.showDeletedSeries}</span>
                </label>
              </div>
            </div>
          </div>
        </header>
        <div className="gmvmax-card__body">
          <SeriesErrorNotice
            error={campaignsQueryEnabled ? campaignsQuery.error : null}
            onRetry={campaignsQueryEnabled ? campaignsQuery.refetch : undefined}
          />
          {campaignsLoading ? <Loading text="加载系列中…" /> : null}
          {!isScopeReady ? (
            <p className="gmvmax-placeholder">{GmvMaxTexts.scopePlaceholder}</p>
          ) : null}
          {campaignsBlockedMessage ? (
            <p className="gmvmax-placeholder">{campaignsBlockedMessage}</p>
          ) : null}
          {campaignsQueryEnabled &&
          !campaignsLoading &&
          !campaignsQuery.error &&
          sortedSeriesRows.length === 0 &&
          (!includeDeletedCampaigns || deletedCampaignCards.length === 0) ? (
            <p className="gmvmax-placeholder">{GmvMaxTexts.noSeriesForScope}</p>
          ) : null}
          {campaignsQueryEnabled ? (
            <div className="gmvmax-series-table">
              <div className="table-wrap">
                <table className="gmvmax-table gmvmax-series-table__table">
                  <thead>
                    <tr>
                      <th>{GmvMaxTexts.seriesName}</th>
                      <th>{GmvMaxTexts.storeLabel}</th>
                      <th>{GmvMaxTexts.statusLabel}</th>
                      <th>消耗</th>
                      <th>GMV</th>
                      <th className="col-actions">{GmvMaxTexts.actionsLabel}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {campaignsLoading ? (
                      <tr>
                        <td colSpan={6}>
                          <Loading text="加载系列数据…" />
                        </td>
                      </tr>
                    ) : null}
                    {!campaignsLoading && sortedSeriesRows.length === 0 ? (
                      <tr>
                        <td colSpan={6}>{GmvMaxTexts.noSeriesForScope}</td>
                      </tr>
                    ) : null}
                    {sortedSeriesRows.map((card) => {
                      const campaignId = card.campaign?.campaign_id || card.campaign?.id;
                      const name =
                        card.campaign?.name ||
                        card.campaign?.campaign_name ||
                        card.detail?.campaign?.name ||
                        `系列 ${campaignId}`;
                      const statusClass = `gmvmax-status-pill gmvmax-status-pill--${card.statusMeta?.tone || 'muted'}`;
                      const performance = card.campaign?.performance;
                      return (
                        <tr key={campaignId}>
                          <td>
                            <div className="gmvmax-series-name">{name}</div>
                          </td>
                          <td>{card.storeName || '—'}</td>
                          <td>
                            <span className={statusClass}>{card.statusMeta?.label || GmvMaxTexts.statusUnknown}</span>
                          </td>
                          <td>{performance?.cost !== undefined && performance?.cost !== null ? `$${formatMoney(performance.cost)}` : '—'}</td>
                          <td>{performance?.gmv !== undefined && performance?.gmv !== null ? `$${formatMoney(performance.gmv)}` : '—'}</td>
                          <td className="col-actions">
                            <div className="gmvmax-series-actions">
                              <button
                                type="button"
                                className="gmvmax-button gmvmax-button--ghost"
                                onClick={() => handleEditRequest(campaignId)}
                              >
                                {GmvMaxTexts.editSeries}
                              </button>
                              <button
                                type="button"
                                className="gmvmax-button gmvmax-button--ghost"
                                onClick={() => handleManage(campaignId)}
                              >
                                {GmvMaxTexts.manageProducts}
                              </button>
                              <button
                                type="button"
                                className="gmvmax-button gmvmax-button--secondary"
                                onClick={() => handleDashboard(campaignId)}
                              >
                                {GmvMaxTexts.viewData}
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}
          {includeDeletedCampaigns && campaignsQueryEnabled ? (
            <div className="gmvmax-card__deleted-section">
              <h3 className="gmvmax-card__subheading">{GmvMaxTexts.deletedSeries}</h3>
              {!campaignsLoading && !campaignsQuery.error && deletedCampaignCards.length === 0 ? (
                <p className="gmvmax-placeholder">{GmvMaxTexts.noDeletedSeries}</p>
              ) : null}
              {deletedCampaignCards.length > 0 ? (
                <ul className="gmvmax-deleted-list">
                  {deletedCampaignCards.map(({ campaign }) => {
                    const campaignId = campaign?.campaign_id || campaign?.id;
                    const name = campaign?.name || campaign?.campaign_name || `系列 ${campaignId}`;
                    return (
                      <li key={campaignId}>
                        <div className="gmvmax-series-name">{name}</div>
                        <span className="gmvmax-deleted-label">{GmvMaxTexts.statusDeleted}</span>
                        <div className="gmvmax-series-actions">
                          <button
                            type="button"
                            className="gmvmax-button gmvmax-button--secondary"
                            onClick={() => handleDashboard(campaignId)}
                          >
                            {GmvMaxTexts.viewData}
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
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
        storeNameById={storeNameById}
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
    </GmvMaxPageErrorBoundary>
  );
}
