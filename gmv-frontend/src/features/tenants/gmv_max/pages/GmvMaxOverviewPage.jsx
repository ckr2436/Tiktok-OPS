import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  useGmvMaxConfigQuery,
  useGmvMaxMetricsQuery,
  useGmvMaxOptionsQuery,
  useGmvMaxBindingStatusQuery,
  useGmvMaxRebindAutoMutation,
  useGmvMaxSyncIntervalQuery,
  useProductsQuery,
  useUpdateGmvMaxSyncIntervalMutation,
  useUpdateGmvMaxCampaignMutation,
  useUpdateGmvMaxStrategyMutation,
} from '../hooks/gmvMaxQueries.js';
import {
  clampPageSize,
  getGmvMaxCampaign,
  getGmvMaxOptions,
} from '../api/gmvMaxApi.js';
import { loadScope, saveScope } from '../utils/scopeStorage.js';

import {
  PROVIDER,
  PROVIDER_LABEL,
  DEFAULT_REPORT_METRICS,
  EMPTY_QUERY_PARAMS,
  DEFAULT_SCOPE,
  formatError,
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
  summariseMetrics,
  summariseMetricsByCampaign,
  formatMoney,
  formatRoi,
  getCampaignStatusMeta,
  isCampaignEnabledStatus,
  extractProductsFromDetail,
  setsEqual,
  toChoiceList,
  extractChoiceList,
} from './gmvMaxOverview/helpers.js';
import {
  formatRangeAsIsoStrings,
  getAdvertiserRecentRange,
  getAdvertiserTodayRange,
  resolveTimezoneLabel,
} from '../utils/timezone.js';
import { SeriesErrorNotice } from './gmvMaxOverview/ErrorHandling.jsx';
import ProductSelectionPanel from './gmvMaxOverview/ProductSelectionPanel.jsx';
import CreateSeriesModal from './gmvMaxOverview/CreateSeriesModal.jsx';
import EditSeriesModal from './gmvMaxOverview/EditSeriesModal.jsx';
import { GmvMaxTexts } from '../locale.js';
import { useGmvSyncTask } from '../hooks/useGmvSyncTask.js';

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
const SYNC_COOLDOWN_MS = 10 * 60 * 1000;

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
  const [lastSyncAt, setLastSyncAt] = useState(null);
  const [advertiserTimezone, setAdvertiserTimezone] = useState(() => resolveTimezoneLabel());
  const [includeDeletedCampaigns, setIncludeDeletedCampaigns] = useState(false);
  const [seriesStatusFilter, setSeriesStatusFilter] = useState('running');
  const [seriesStoreFilter, setSeriesStoreFilter] = useState('');
  const [seriesSearch, setSeriesSearch] = useState('');
  const [sortOption, setSortOption] = useState('latest');
  const [hasLoadedScope, setHasLoadedScope] = useState(false);
  const autoOptionsRefreshAccounts = useRef(new Set());
  const syncInFlightRef = useRef(false);
  const performSyncRef = useRef(null);
  const lastAutoSyncScopeRef = useRef('');

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
        queryFn: () => getGmvMaxOptions(workspaceId, provider, accountAuthId, scopeOptionsParams),
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

  useEffect(() => {
    if (!storeId) {
      lastAutoSyncScopeRef.current = '';
    }
  }, [storeId]);

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

  useEffect(() => {
    setAdvertiserTimezone(resolveTimezoneLabel(advertiserTimezoneFromOptions));
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
    bindingConfigLoading,
    bindingConfigFetching,
  });

  const campaignsBlockedMessage = useMemo(() => {
    if (!isScopeReady || campaignsQueryEnabled) return '';
    if (bindingConfigLoading || bindingConfigFetching) {
      return '绑定配置加载中…';
    }
    if (!autoBindingVerified) {
      return '正在等待自动绑定验证完成后再加载 GMV Max 系列…';
    }
    return '';
  }, [
    bindingConfigFetching,
    bindingConfigLoading,
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

  const productParams = useMemo(
    () => ({
      store_id: storeId || undefined,
      advertiser_id: advertiserId || undefined,
      owner_bc_id: businessCenterId || undefined,
      page_size: clampPageSize(50),
    }),
    [advertiserId, businessCenterId, storeId],
  );

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    productParams,
    {
      enabled: Boolean(workspaceId && provider && isScopeReady),
    },
  );

  const campaignParams = useMemo(() => {
    const params = { page_size: clampPageSize(50) };
    if (businessCenterId) params.owner_bc_id = businessCenterId;
    if (advertiserId) params.advertiser_id = advertiserId;
    if (storeId) params.store_ids = [String(storeId)];
    if (includeDeletedCampaigns) params.include_deleted = 1;
    return params;
  }, [advertiserId, businessCenterId, includeDeletedCampaigns, storeId]);

  const campaignsQuery = useGmvMaxCampaignsQuery(
    workspaceId,
    provider,
    authId,
    campaignParams,
    {
      enabled: campaignsQueryEnabled,
    },
  );

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
    if (bindingConfigLoading || bindingConfigFetching) {
      return { variant: 'muted', message: '绑定配置加载中…' };
    }
    if (bindingConfigError) {
      return {
        variant: 'error',
        message: `Failed to load binding configuration: ${formatError(bindingConfigError)}`,
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
    return { variant: 'success', message: '绑定已确认，可开始同步 GMV Max。' };
  }, [
    bindingConfigError,
    bindingConfigFetching,
    bindingConfigLoading,
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
    setSelectedProductIds([]);
  }, [advertiserId, authId, businessCenterId, storeId, workspaceId]);

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
    if (!isScopeReady) {
      setSelectedProductIds([]);
    }
  }, [isScopeReady]);

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

  const metricsRange = useMemo(
    () => getAdvertiserRecentRange(7, advertiserTimezone),
    [advertiserTimezone],
  );

  const metricsRangeParams = useMemo(
    () => ({
      ...formatRangeAsIsoStrings(metricsRange),
      store_ids: storeId ? [String(storeId)] : undefined,
    }),
    [metricsRange, storeId],
  );

  const todayRange = useMemo(() => getAdvertiserTodayRange(advertiserTimezone), [advertiserTimezone]);
  const todayRangeParams = useMemo(
    () => ({
      ...formatRangeAsIsoStrings(todayRange),
      store_ids: storeId ? [String(storeId)] : undefined,
    }),
    [storeId, todayRange],
  );

  const overallMetricsQuery = useGmvMaxMetricsQuery(
    workspaceId,
    provider,
    authId,
    'all',
    {
      start_date: metricsRangeParams.start_date,
      end_date: metricsRangeParams.end_date,
      store_ids: metricsRangeParams.store_ids,
    },
    {
      enabled: Boolean(workspaceId && authId && storeId && campaignsQueryEnabled),
      staleTime: 60 * 1000,
      refetchInterval: autoRefreshMs,
    },
  );
  const overallReport =
    overallMetricsQuery.data?.report || overallMetricsQuery.data?.data || overallMetricsQuery.data || null;
  const overallSummary = useMemo(() => {
    if (!overallReport) return null;
    return summariseMetrics(overallReport);
  }, [overallReport]);
  const metricsByCampaign = useMemo(
    () => (overallReport ? summariseMetricsByCampaign(overallReport) : new Map()),
    [overallReport],
  );

  const todayMetricsQuery = useGmvMaxMetricsQuery(
    workspaceId,
    provider,
    authId,
    'all',
    {
      start_date: todayRangeParams.start_date,
      end_date: todayRangeParams.end_date,
      store_ids: todayRangeParams.store_ids,
    },
    {
      enabled: Boolean(workspaceId && authId && storeId && campaignsQueryEnabled),
      staleTime: 60 * 1000,
      refetchInterval: autoRefreshMs,
    },
  );
  const todayReport =
    todayMetricsQuery.data?.report || todayMetricsQuery.data?.data || todayMetricsQuery.data || null;
  const todayMetricsByCampaign = useMemo(
    () => (todayReport ? summariseMetricsByCampaign(todayReport) : new Map()),
    [todayReport],
  );

  const campaignDetailQueries = useQueries({
    queries: campaignsQueryEnabled
      ? campaigns.map((campaign) => {
          const campaignId = campaign?.campaign_id || campaign?.id;
          return {
            queryKey: [
              'gmvMax',
              'campaign-detail',
              workspaceId,
              provider,
              authId,
              businessCenterId,
              advertiserId,
              storeId,
              campaignId,
            ],
            queryFn: () => getGmvMaxCampaign(workspaceId, provider, authId, campaignId),
            enabled: Boolean(workspaceId && authId && campaignId && campaignsQueryEnabled),
            staleTime: 60 * 1000,
          };
        })
      : [],
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
          scopeFallback: campaignScopeSnapshot,
        };
      }),
    [campaignDetailsById, campaignScopeSnapshot, campaigns],
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
    const metricMap = metricsByCampaign || new Map();
    return filteredCampaignCards.map((card) => {
      const campaignId = card.campaign?.campaign_id || card.campaign?.id;
      const statusMeta = getCampaignStatusMeta(
        card.campaign?.operation_status ||
          card.campaign?.status ||
          card.detail?.campaign?.operation_status ||
          card.detail?.campaign?.status,
      );
      const createdAt =
        Date.parse(card.campaign?.created_time || card.campaign?.create_time || card.campaign?.createdAt || '') ||
        parseFloat(card.campaign?.created_time || card.campaign?.create_time || card.campaign?.createdAt || '0') ||
        0;
      return {
        ...card,
        statusMeta,
        createdAt,
        storeName: resolveStoreName(card.campaign),
        metricsSummary: campaignId ? metricMap.get(String(campaignId)) || null : null,
        metricsLoading: overallMetricsQuery.isLoading,
        metricsError: overallMetricsQuery.error,
      };
    });
  }, [filteredCampaignCards, metricsByCampaign, overallMetricsQuery.error, overallMetricsQuery.isLoading, resolveStoreName]);

  const seriesRows = useMemo(() => {
    const search = seriesSearch.trim().toLowerCase();
    return campaignCardsWithMeta
      .map((card) => {
        const campaignId = card.campaign?.campaign_id || card.campaign?.id;
        return {
          ...card,
          todaySummary: campaignId ? todayMetricsByCampaign.get(String(campaignId)) || null : null,
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
  }, [campaignCardsWithMeta, seriesSearch, seriesStatusFilter, seriesStoreFilter, todayMetricsByCampaign]);

  const sortedSeriesRows = useMemo(() => {
    const list = [...seriesRows];
    const getRoas = (card) => card.todaySummary?.roas ?? card.metricsSummary?.roas ?? -Infinity;
    const getGmv = (card) => card.todaySummary?.gmv ?? card.metricsSummary?.gmv ?? 0;
    const getSpend = (card) => card.todaySummary?.spend ?? card.metricsSummary?.spend ?? 0;
    const getCreated = (card) => (Number.isFinite(card.createdAt) ? card.createdAt : 0);
    list.sort((a, b) => {
      if (sortOption === 'roas') return getRoas(b) - getRoas(a);
      if (sortOption === 'gmv') return getGmv(b) - getGmv(a);
      if (sortOption === 'spend') return getSpend(b) - getSpend(a);
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

  const syncTask = useGmvSyncTask({ workspaceId, provider, authId });
  const rebindAutoMutation = useGmvMaxRebindAutoMutation(workspaceId, provider, authId);

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

  const performCampaignSync = useCallback(async () => {
    const normalizedBcId = businessCenterId ? String(businessCenterId) : undefined;
    const normalizedStoreId = storeId ? String(storeId) : undefined;
    const payload = {
      owner_bc_id: normalizedBcId,
      bc_id: normalizedBcId,
      advertiser_id: advertiserId ? String(advertiserId) : undefined,
      store_id: normalizedStoreId,
      campaign_filter: normalizedStoreId ? { store_ids: [normalizedStoreId] } : undefined,
      campaign_options: { page_size: clampPageSize(50) },
      report: {
        store_ids: normalizedStoreId ? [normalizedStoreId] : undefined,
        start_date: metricsRangeParams.start_date,
        end_date: metricsRangeParams.end_date,
        metrics: DEFAULT_REPORT_METRICS,
        dimensions: ['campaign_id', 'stat_time_day'],
        enable_total_metrics: true,
      },
    };

    const result = await syncTask.runSync(payload);
    if (result?.state === 'SUCCESS') {
      await refreshScopeQueries();
      return 'SUCCESS';
    }
    throw new Error('同步失败，请稍后再试。');
  }, [
    advertiserId,
    authId,
    businessCenterId,
    metricsRangeParams,
    refreshScopeQueries,
    storeId,
    syncTask,
  ]);

  const performSync = useCallback(async () => {
    if (!workspaceId || !provider || !authId) return;
    let nextNotice = null;
    let nextError = null;
    const now = Date.now();

    if (!isScopeReady) {
      setSyncNotice(null);
      setSyncError('请先选择店铺以完成数据同步。');
      return;
    }
    if (bindingConfigLoading || bindingConfigFetching) {
      setSyncNotice(null);
      setSyncError('绑定配置加载中，请稍后再试。');
      return;
    }
    if (!bindingReady) {
      setSyncNotice(null);
      setSyncError('请先完成店铺-广告主绑定后再同步 GMV Max 数据。');
      return;
    }
    if (lastSyncAt && now - lastSyncAt < SYNC_COOLDOWN_MS) {
      setSyncNotice(null);
      setSyncError('同步请求过于频繁，请稍后再试。');
      return;
    }
    if (syncInFlightRef.current || isSyncing || syncTask.isSyncing) return;

    syncInFlightRef.current = true;
    setSyncNotice(null);
    setSyncError(null);
    setIsSyncing(true);
    setLastSyncAt(now);
    try {
      const finalState = await performCampaignSync();
      if (finalState === 'SUCCESS') {
        nextNotice = { variant: 'success', message: '同步完成，数据已刷新。' };
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
    advertiserId,
    authId,
    autoBindingVerified,
    bindingConfigFetching,
    bindingConfigLoading,
    businessCenterId,
    isScopeReady,
    isSyncing,
    lastSyncAt,
    performCampaignSync,
    provider,
    syncTask,
    storeId,
    workspaceId,
  ]);

  useEffect(() => {
    performSyncRef.current = performSync;
  }, [performSync]);

  useEffect(() => {
    if (!isScopeReady || !workspaceId || !provider || !authId || !storeId) return;
    const scopeKey = `${workspaceId}:${provider}:${authId}:${storeId}`;
    if (lastAutoSyncScopeRef.current === scopeKey) return;
    lastAutoSyncScopeRef.current = scopeKey;
    if (typeof performSyncRef.current === 'function') {
      performSyncRef.current();
    }
  }, [authId, isScopeReady, provider, storeId, workspaceId]);

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

  const handleEditRequest = useCallback((campaignId) => {
    setEditingCampaignId(String(campaignId));
  }, []);

  const handleCloseEdit = useCallback(() => {
    setEditingCampaignId('');
  }, []);

  const handleSeriesUpdated = useCallback(() => {
    setEditingCampaignId('');
    refreshScopeQueries();
  }, [
    authId,
    provider,
    refreshScopeQueries,
    workspaceId,
  ]);

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

  const campaignsLoading = Boolean(
    campaignsQueryEnabled && (campaignsQuery.isLoading || campaignsQuery.isFetching),
  );
  const productsLoading = Boolean(isScopeReady && (productsQuery.isLoading || productsQuery.isFetching));
  const balanceTimestamp = advertiserBalance?.fetched_at;
  const canDisplayBalance = Boolean(storeId && bindingConfigMatchedScope);
  const isSyncIntervalUpdating = Boolean(
    updateSyncIntervalMutation?.isPending || updateSyncIntervalMutation?.isLoading,
  );

  return (
    <div className="gmvmax-page">
      {isSyncing ? (
        <div className="gmvmax-status-banner gmvmax-status-banner--muted">数据同步中…</div>
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
      <header className="gmvmax-page__header">
        <div>
          <h1>{GmvMaxTexts.overviewTitle}</h1>
          <p className="gmvmax-page__subtitle">{GmvMaxTexts.overviewSubtitle}</p>
        </div>
        <div className="gmvmax-page__header-actions">
          <span className="gmvmax-provider-badge">{`${GmvMaxTexts.providerLabel}：${PROVIDER_LABEL}`}</span>
          <button
            type="button"
            className="gmvmax-button gmvmax-button--primary"
            onClick={performSync}
            disabled={
              isSyncing ||
              !isScopeReady ||
              bindingConfigLoading ||
              bindingConfigFetching ||
              !bindingReady
            }
            title={bindingReady ? undefined : '请先完成店铺-广告主绑定'}
          >
            {isSyncing ? '同步中…' : '同步数据'}
          </button>
          <div className="gmvmax-balance-chip">
            <div className="gmvmax-balance-chip__row">
              <div>
                <p className="gmvmax-balance-chip__title">{GmvMaxTexts.advertiserBalance}</p>
                <p className="gmvmax-balance-chip__timestamp">
                  {canDisplayBalance && balanceTimestamp
                    ? `${GmvMaxTexts.balanceUpdatedPrefix} ${formatISODate(balanceTimestamp)}`
                    : GmvMaxTexts.balanceUnavailable}
                </p>
                {advertiserTimezone ? (
                  <p className="gmvmax-balance-chip__timezone">{`按广告主时区：${advertiserTimezone}`}</p>
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
                </>
              ) : (
                <span className="gmvmax-balance-chip__placeholder">{GmvMaxTexts.awaitingBalance}</span>
              )}
            </div>
          </div>
        </div>
      </header>

      <section className="gmvmax-card gmvmax-card--filters">
        <header className="gmvmax-card__header">
          <div>
            <h2>{GmvMaxTexts.scopeFilters}</h2>
            <p>{GmvMaxTexts.scopeDescription}</p>
          </div>
        </header>
        <div className="gmvmax-card__body">
          <div className="gmvmax-field-grid gmvmax-field-grid--two">
            <FormField label="数据自动刷新间隔">
              <div>
                <select
                  value={autoRefreshInterval}
                  onChange={handleAutoRefreshChange}
                  disabled={isSyncIntervalUpdating || syncIntervalQuery.isLoading}
                >
                  {refreshIntervalOptions.map((option) => (
                    <option key={option} value={option}>{`${option} 分钟`}</option>
                  ))}
                </select>
                <p className="gmvmax-subtext">
                  自动刷新间隔（当前 {autoRefreshInterval} 分钟），针对轻量级指标数据，不会触发后端全量同步。
                </p>
              </div>
            </FormField>
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
          </div>
          <div className={scopeStatusClassName}>{scopeStatus.message}</div>
        </div>
      </section>

      {campaignsQueryEnabled ? (
        <section className="gmvmax-card gmvmax-card--summary">
          <header className="gmvmax-card__header">
            <h2>{GmvMaxTexts.summaryBarTitle}</h2>
            {overallMetricsQuery.isLoading ? <Loading text="汇总指标加载中…" /> : null}
          </header>
          <div className="gmvmax-card__body">
            <div className="gmvmax-overview-summary">
              <div>
                <p>{GmvMaxTexts.totalSpend}</p>
                <strong>{overallSummary ? formatMoney(overallSummary.spend) : '—'}</strong>
              </div>
              <div>
                <p>{GmvMaxTexts.totalGmv}</p>
                <strong>{overallSummary ? formatMoney(overallSummary.gmv) : '—'}</strong>
              </div>
              <div>
                <p>{GmvMaxTexts.averageRoas}</p>
                <strong>
                  {overallSummary && overallSummary.roas !== null ? formatRoi(overallSummary.roas) : '—'}
                </strong>
              </div>
              <div>
                <p>{GmvMaxTexts.totalOrders}</p>
                <strong>{overallSummary ? overallSummary.orders : '—'}</strong>
              </div>
            </div>
            {overallMetricsQuery.error ? (
              <p className="gmvmax-placeholder">汇总指标加载失败，请稍后重试。</p>
            ) : null}
          </div>
        </section>
      ) : null}

      <section className="gmvmax-card">
        <header className="gmvmax-card__header">
          <h2>{GmvMaxTexts.gmvMaxSeries}</h2>
          <div className="gmvmax-card__header-actions gmvmax-card__header-actions--wrap">
            <button
              type="button"
              className="gmvmax-button gmvmax-button--primary"
              onClick={handleOpenCreate}
              disabled={!canCreateSeries}
            >
              {GmvMaxTexts.createSeries}
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
                    <option value="spend">{GmvMaxTexts.sortTodaySpend}</option>
                    <option value="roas">{GmvMaxTexts.sortBestRoas}</option>
                    <option value="gmv">{GmvMaxTexts.sortBestGmv}</option>
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
                      <th>{GmvMaxTexts.todaySpend}</th>
                      <th>{GmvMaxTexts.todayGmv}</th>
                      <th>ROAS</th>
                      <th className="col-actions">{GmvMaxTexts.actionsLabel}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {campaignsLoading || todayMetricsQuery.isLoading ? (
                      <tr>
                        <td colSpan={7}>
                          <Loading text="加载系列数据…" />
                        </td>
                      </tr>
                    ) : null}
                    {!campaignsLoading && sortedSeriesRows.length === 0 ? (
                      <tr>
                        <td colSpan={7}>{GmvMaxTexts.noSeriesForScope}</td>
                      </tr>
                    ) : null}
                    {sortedSeriesRows.map((card) => {
                      const campaignId = card.campaign?.campaign_id || card.campaign?.id;
                      const name =
                        card.campaign?.name ||
                        card.campaign?.campaign_name ||
                        card.detail?.campaign?.name ||
                        `系列 ${campaignId}`;
                      const todaySpend = card.todaySummary?.spend ?? null;
                      const todayGmv = card.todaySummary?.gmv ?? null;
                      const roas =
                        card.todaySummary?.roas ?? card.metricsSummary?.roas ??
                        (todaySpend && todaySpend > 0 ? (todayGmv || 0) / todaySpend : null);
                      const statusClass = `gmvmax-status-pill gmvmax-status-pill--${card.statusMeta?.tone || 'muted'}`;
                      return (
                        <tr key={campaignId}>
                          <td>
                            <div className="gmvmax-series-name">{name}</div>
                          </td>
                          <td>{card.storeName || '—'}</td>
                          <td>
                            <span className={statusClass}>{card.statusMeta?.label || GmvMaxTexts.statusUnknown}</span>
                          </td>
                          <td>{todaySpend === null ? '—' : `$${formatMoney(todaySpend)}`}</td>
                          <td>{todayGmv === null ? '—' : `$${formatMoney(todayGmv)}`}</td>
                          <td>{roas === null ? '—' : formatRoi(roas)}</td>
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
                        <span className="gmvmax-deleted-label">{GmvMaxTexts.statusEnded}</span>
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
  );
}
