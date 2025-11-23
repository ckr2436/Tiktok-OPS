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
  useGmvMaxAutoBindingMutation,
  useProductsQuery,
  useSyncAccountMetadataMutation,
  useSyncAccountProductsMutation,
  useSyncGmvMaxCampaignsMutation,
  useUpdateGmvMaxCampaignMutation,
  useUpdateGmvMaxStrategyMutation,
} from '../hooks/gmvMaxQueries.js';
import {
  clampPageSize,
  getGmvMaxCampaign,
  getGmvMaxOptions,
  getGmvMaxSyncStatus,
} from '../api/gmvMaxApi.js';
import { loadScope, saveScope } from '../utils/scopeStorage.js';
import {
  MAX_SCOPE_PRESETS,
  buildScopePresetId,
  loadScopePresets,
  saveScopePresets,
} from '../utils/scopePresets.js';

import {
  PROVIDER,
  PROVIDER_LABEL,
  DEFAULT_REPORT_METRICS,
  EMPTY_QUERY_PARAMS,
  DEFAULT_SCOPE,
  formatMetaSummary,
  formatError,
  formatISODate,
  getRecentDateRange,
  getProductIdentifier,
  getProductAvailabilityStatus,
  isProductAvailable,
  getAvailableProductIds,
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
  collectProductIdsFromList,
  collectProductIdsFromCampaign,
  collectProductIdsFromDetail,
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
  filterCampaignsByStatus,
  parseOptionalFloat,
  summariseMetrics,
  formatMoney,
  formatRoi,
  formatCampaignStatus,
  isCampaignEnabledStatus,
  extractProductsFromDetail,
  setsEqual,
  toChoiceList,
  extractChoiceList,
} from './gmvMaxOverview/helpers.js';
import { ErrorBlock, SeriesErrorNotice } from './gmvMaxOverview/ErrorHandling.jsx';
import ProductSelectionPanel from './gmvMaxOverview/ProductSelectionPanel.jsx';
import CampaignCard from './gmvMaxOverview/CampaignCard.jsx';
import CreateSeriesModal from './gmvMaxOverview/CreateSeriesModal.jsx';
import EditSeriesModal from './gmvMaxOverview/EditSeriesModal.jsx';

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

export default function GmvMaxOverviewPage() {
  const { wid: workspaceId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const provider = PROVIDER;
  const [scope, setScope] = useState(() => ({ ...DEFAULT_SCOPE }));
  const [selectedProductIds, setSelectedProductIds] = useState([]);
  const [isCreateModalOpen, setCreateModalOpen] = useState(false);
  const [editingCampaignId, setEditingCampaignId] = useState('');
  const [syncError, setSyncError] = useState(null);
  const [isSyncPolling, setIsSyncPolling] = useState(false);
  const [metaSyncMessage, setMetaSyncMessage] = useState('');
  const [metaSyncError, setMetaSyncError] = useState(null);
  const [productSyncMessage, setProductSyncMessage] = useState('');
  const [productSyncError, setProductSyncError] = useState(null);
  const [autoBindingStatus, setAutoBindingStatus] = useState(null);
  const [hasLoadedScope, setHasLoadedScope] = useState(false);
  const [scopePresets, setScopePresets] = useState([]);
  const [selectedPresetId, setSelectedPresetId] = useState('');
  const [presetLabelInput, setPresetLabelInput] = useState('');
  const autoOptionsRefreshAccounts = useRef(new Set());
  const autoBindingKeyRef = useRef('');

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

  const authId = scope.accountAuthId ? String(scope.accountAuthId) : '';
  const businessCenterId = scope.bcId ? String(scope.bcId) : '';
  const advertiserId = scope.advertiserId ? String(scope.advertiserId) : '';
  const storeId = scope.storeId ? String(scope.storeId) : '';
  const isScopeReady = Boolean(authId && businessCenterId && advertiserId && storeId);
  const scopeOptionsParams = EMPTY_QUERY_PARAMS;
  const scopeOptionsQueryKey = useMemo(
    () => ['gmvMax', 'options', workspaceId, provider, authId, scopeOptionsParams],
    [authId, provider, scopeOptionsParams, workspaceId],
  );
  const accountsQueryKey = useMemo(
    () => ['gmvMax', 'accounts', workspaceId, provider, EMPTY_QUERY_PARAMS],
    [provider, workspaceId],
  );

  useEffect(() => {
    if (!workspaceId) {
      setScope({ ...DEFAULT_SCOPE });
      setHasLoadedScope(false);
      setSelectedPresetId('');
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
    setMetaSyncMessage('');
    setMetaSyncError(null);
  }, [authId]);

  useEffect(() => {
    if (!workspaceId) {
      setScopePresets([]);
      setSelectedPresetId('');
      return;
    }
    const presets = loadScopePresets(workspaceId);
    setScopePresets(presets);
    setSelectedPresetId('');
  }, [workspaceId]);

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
    },
  );

  const bindingConfig = bindingConfigQuery.data || null;
  const bindingConfigLoading = bindingConfigQuery.isLoading;
  const bindingConfigFetching = bindingConfigQuery.isFetching;
  const bindingConfigError = bindingConfigQuery.error;
  const savedBusinessCenterId = bindingConfig?.bc_id ? String(bindingConfig.bc_id) : '';
  const savedAdvertiserId = bindingConfig?.advertiser_id ? String(bindingConfig.advertiser_id) : '';
  const savedStoreId = bindingConfig?.store_id ? String(bindingConfig.store_id) : '';
  const bindingConfigMatchedScope = bindingConfigMatchesScope(bindingConfig, {
    storeId,
    businessCenterId,
    advertiserId,
  });

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

  const autoBindingVerified =
    autoBindingStatus?.variant === 'success' || bindingConfigMatchedScope === true;

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
      return 'Loading binding configuration…';
    }
    if (!autoBindingVerified) {
      return 'Waiting for auto binding verification before loading GMV Max series…';
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
    return params;
  }, [advertiserId, businessCenterId, storeId]);

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
        message: 'Select a store to configure the GMV Max binding.',
      };
    }
    if (bindingConfigLoading) {
      return { variant: 'muted', message: 'Loading binding configuration…' };
    }
    if (bindingConfigError) {
      return {
        variant: 'error',
        message: `Failed to load binding configuration: ${formatError(bindingConfigError)}`,
      };
    }
    if (!autoBindingVerified) {
      return {
        variant: 'warning',
        message: 'Auto binding in progress. Verification is required before syncing.',
      };
    }
    return { variant: 'success', message: 'Auto binding verified. You can sync GMV Max now.' };
  }, [
    bindingConfigError,
    bindingConfigLoading,
    autoBindingVerified,
    storeId,
  ]);
  const scopeStatusClassName = `gmvmax-status-banner gmvmax-status-banner--${scopeStatus.variant || 'muted'}`;

  const defaultPresetLabel = useMemo(() => {
    const parts = [
      selectedAccountLabel,
      selectedBusinessCenterLabel,
      selectedAdvertiserLabel,
      selectedStoreLabel,
    ].filter(Boolean);
    return parts.join(' / ');
  }, [
    selectedAccountLabel,
    selectedAdvertiserLabel,
    selectedBusinessCenterLabel,
    selectedStoreLabel,
  ]);

  const handleAccountChange = useCallback((event) => {
    const value = event?.target?.value || '';
    setScope({
      accountAuthId: value ? String(value) : null,
      bcId: null,
      advertiserId: null,
      storeId: null,
    });
    setSelectedPresetId('');
  }, []);

  const handleStoreChange = useCallback((event) => {
    const value = event?.target?.value || '';
    const matched = storeOptions.find((option) => option.value === value);
    if (matched?.needsAuthorization) {
      setAutoBindingStatus({
        variant: 'warning',
        message: `Store ${matched.label} has no effective advertiser authorization. Please authorize an advertiser.`,
      });
    } else {
      setAutoBindingStatus(null);
    }
    setScope((prev) => ({
      ...prev,
      accountAuthId: matched?.authId || prev.accountAuthId,
      advertiserId: matched?.advertiserId ? String(matched.advertiserId) : null,
      bcId: matched?.bcId ? String(matched.bcId) : null,
      storeId: value ? String(value) : null,
    }));
    setSelectedPresetId('');
  }, [storeOptions]);

  const handlePresetChange = useCallback(
    (event) => {
      const presetId = event?.target?.value || '';
      setSelectedPresetId(presetId);
      const preset = scopePresets.find((item) => item.id === presetId);
      if (!preset) return;
      setScope({
        accountAuthId: preset.accountAuthId || null,
        bcId: preset.bcId || null,
        advertiserId: preset.advertiserId || null,
        storeId: preset.storeId || null,
      });
    },
    [scopePresets],
  );

  const handleDeletePreset = useCallback(() => {
    if (!workspaceId || !selectedPresetId) return;
    setScopePresets((prev) => {
      const next = prev.filter((preset) => preset.id !== selectedPresetId);
      saveScopePresets(workspaceId, next);
      return next;
    });
    setSelectedPresetId('');
  }, [selectedPresetId, workspaceId]);

  const handleSavePreset = useCallback(() => {
    if (!workspaceId || !isScopeReady) return;
    const label = presetLabelInput.trim() || defaultPresetLabel || 'GMV Max scope preset';
    const preset = {
      id: buildScopePresetId({
        accountAuthId: authId,
        bcId: businessCenterId,
        advertiserId,
        storeId,
      }),
      label,
      accountAuthId: authId,
      bcId: businessCenterId,
      advertiserId,
      storeId,
    };
    setScopePresets((prev) => {
      const filtered = prev.filter((item) => item.id !== preset.id);
      const next = [preset, ...filtered].slice(0, MAX_SCOPE_PRESETS);
      saveScopePresets(workspaceId, next);
      return next;
    });
    setPresetLabelInput('');
    setSelectedPresetId(preset.id);
  }, [
    advertiserId,
    authId,
    businessCenterId,
    defaultPresetLabel,
    isScopeReady,
    presetLabelInput,
    storeId,
    workspaceId,
  ]);

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
    setSelectedPresetId('');
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
    setSelectedPresetId('');
  }, [advertiserId, advertiserOptions, businessCenterId, scopeOptionsReady]);

  useEffect(() => {
    if (!storeId || !scopeOptionsReady) return;
    const hasStore = storeOptions.some((option) => option.value === storeId);
    if (hasStore) return;
    setScope((prev) => ({
      ...prev,
      storeId: null,
    }));
    setSelectedPresetId('');
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
    return filterCampaignsByStatus(Array.isArray(items) ? items : []);
  }, [campaignsQuery.data, campaignsQueryEnabled]);

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

  // 根据 GMV Max 系列的启用状态收集被占用的产品 ID。
  // 只有当系列的 operation_status 为启用（通过 isCampaignEnabledStatus 判断）时，
  // 才认为其中的产品被占用。否则即便产品在该系列下，也视为未占用。
  const occupiedProductIds = useMemo(() => {
    const ids = new Set();
    // 遍历当前范围内的所有系列，只收集启用系列中的产品 ID。
    campaigns.forEach((campaign) => {
      // campaign.operation_status 或 campaign.operationStatus 表示系列是否启用
      const status = campaign?.operation_status ?? campaign?.operationStatus;
      if (isCampaignEnabledStatus(status)) {
        collectProductIdsFromCampaign(campaign, ids);
      }
    });
    // 同样地，从详情结果中收集启用系列中的产品 ID
    campaignDetailQueries.forEach((result) => {
      const detail = result?.data;
      if (!detail) return;
      const detailStatus =
        detail?.campaign?.operation_status ?? detail?.campaign?.operationStatus;
      if (isCampaignEnabledStatus(detailStatus)) {
        collectProductIdsFromDetail(detail, ids);
      }
    });
    return ids;
  }, [campaignDetailQueries, campaigns]);

  const productsWithAvailability = useMemo(() => {
    if (!isScopeReady || products.length === 0) return [];

    return products.map((product) => {
      const id = getProductIdentifier(product); // 获取产品标识
      if (!id) return product; // 如果没有标识符则跳过该产品

    // 判断产品是否已被 GMV Max 占用（仅在启用的系列中才视为占用）
      const isOccupied = occupiedProductIds.has(id);
      const nextAdsStatus = isOccupied ? 'OCCUPIED' : 'UNOCCUPIED';  // 设定占用状态

      // 如果当前状态已经是正确的 GMV Max 占用状态，无需更改
      if (String(product.gmv_max_ads_status || '').toUpperCase() === nextAdsStatus) {
        return product; // 如果状态一致，直接返回原产品
      }

      // 返回修改后的产品对象
      return {
        ...product,
        gmv_max_ads_status: nextAdsStatus,  // 修改 GMV Max 占用状态
      };
    });
  }, [occupiedProductIds, isScopeReady, products]);

  const unassignedProducts = useMemo(() => {
    if (!isScopeReady || productsWithAvailability.length === 0) return [];

    return productsWithAvailability.filter((product) => {
      const id = getProductIdentifier(product); // 获取产品标识
      if (!id) return false;  // 如果没有标识符则跳过该产品

      // 判断产品是否未被 GMV Max 占用
      const isNotOccupied = product.gmv_max_ads_status === 'UNOCCUPIED';

      // 判断产品是否可用
      const isAvailable = isProductAvailable(product);

      // 只有未被占用且可用的产品才会被视为有效
      return isNotOccupied && isAvailable;
    });
  // 依赖项中不需要 occupiedProductIds，因为占用状态已经体现在 productsWithAvailability 中
  }, [isScopeReady, productsWithAvailability]);

  useEffect(() => {
    setSelectedProductIds((prev) => {
      if (!Array.isArray(prev) || prev.length === 0) return prev;
      const availableIds = new Set(
        unassignedProducts.map((product) => getProductIdentifier(product)).filter(Boolean),
      );
      const filtered = prev.filter((id) => availableIds.has(String(id)));
      return filtered.length === prev.length ? prev : filtered;
    });
  }, [unassignedProducts]);

  const selectedProductIdSet = useMemo(
    () => new Set((selectedProductIds || []).map((id) => String(id))),
    [selectedProductIds],
  );

  const handleToggleProduct = useCallback((id) => {
    setSelectedProductIds((prev) => {
      const next = new Set((prev || []).map((value) => String(value)));
      const key = String(id);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return Array.from(next);
    });
  }, []);

  const handleToggleAllProducts = useCallback(
    (ids) => {
      setSelectedProductIds((prev) => {
        const next = new Set((prev || []).map((value) => String(value)));
        const normalized = (ids || []).map(String);
        const shouldDeselect = normalized.every((id) => next.has(id));
        if (shouldDeselect) {
          normalized.forEach((id) => next.delete(id));
        } else {
          normalized.forEach((id) => next.add(id));
        }
        return Array.from(next);
      });
    },
    [],
  );

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
      return matches && !pending;
    });
  }, [
    advertiserId,
    businessCenterId,
    campaignCards,
    campaignsQueryEnabled,
    storeId,
  ]);

  const metadataSyncMutation = useSyncAccountMetadataMutation(workspaceId, provider, authId);
  const productSyncMutation = useSyncAccountProductsMutation(workspaceId, provider, authId);
  const syncMutation = useSyncGmvMaxCampaignsMutation(workspaceId, provider, authId);
  const autoBindingMutation = useGmvMaxAutoBindingMutation(workspaceId, provider, authId);

  useEffect(() => {
    if (!bindingConfigMatchedScope || autoBindingMutation.isPending) return;
    setAutoBindingStatus((prev) => {
      if (prev?.variant === 'success') return prev;
      return {
        variant: 'success',
        message: 'Confirmed GMV Max binding from saved configuration. You can sync GMV Max now.',
      };
    });
    if (!autoBindingKeyRef.current && authId && storeId) {
      autoBindingKeyRef.current = `${authId}:store:${storeId}`;
    }
  }, [authId, autoBindingMutation.isPending, bindingConfigMatchedScope, storeId]);

  useEffect(() => {
    if (!workspaceId || !provider || !authId || !storeId) {
      setAutoBindingStatus(null);
      autoBindingKeyRef.current = '';
      return;
    }
    if (!scopeOptionsReady) {
      setAutoBindingStatus(null);
      autoBindingKeyRef.current = '';
      return;
    }
    const derivedAdvertiserId = advertiserId || storeToAdvertiserId.get(storeId) || '';
    const key = storeId && authId ? `${authId}:store:${storeId}` : '';
    if (autoBindingMutation.isPending) {
      return;
    }
    if (autoBindingKeyRef.current === key && !autoBindingMutation.isError) {
      return;
    }
    autoBindingKeyRef.current = key;
    let cancelled = false;
    setAutoBindingStatus({ variant: 'muted', message: 'Auto-detecting GMV Max binding…' });
    (async () => {
      try {
        const response = await autoBindingMutation.mutateAsync({
          store_id: storeId,
          persist: true,
        });
        if (cancelled) return;
        const candidates = Array.isArray(response?.candidates) ? response.candidates : [];
        const selected = (() => {
          if (response?.selected) return response.selected;
          const ready = candidates.find((candidate) => {
            const status = (candidate?.authorization_status || '').toUpperCase();
            const hasBc = Boolean(candidate?.store_authorized_bc_id);
            const usageAllowed = candidate?.promote_all_products_allowed !== false;
            const occupancyOk = candidate?.is_running_custom_shop_ads !== true;
            return hasBc && usageAllowed && occupancyOk && (status === 'EFFECTIVE' || !status);
          });
          const effectiveCandidate = candidates.find(
            (candidate) => (candidate?.authorization_status || '').toUpperCase() === 'EFFECTIVE',
          );
          const authorizedCandidate = candidates.find(
            (candidate) => (candidate?.authorization_status || '').toUpperCase() !== 'UNAUTHORIZED',
          );
          return ready || effectiveCandidate || authorizedCandidate || candidates[0] || null;
        })();
        if (!selected) {
          setAutoBindingStatus({
            variant: 'warning',
            message: 'Unable to confirm GMV Max exclusive authorization for the selected store.',
          });
          return;
        }
        const nextAdvertiserId = selected.advertiser_id || derivedAdvertiserId;
        const nextBusinessCenterId = selected.store_authorized_bc_id || businessCenterId;
        setScope((prev) => {
          const updatedAdvertiser = nextAdvertiserId || prev.advertiserId;
          const updatedBcId = nextBusinessCenterId || prev.bcId;
          if (updatedAdvertiser === prev.advertiserId && updatedBcId === prev.bcId) {
            return prev;
          }
          return {
            ...prev,
            advertiserId: updatedAdvertiser || null,
            bcId: updatedBcId || null,
          };
        });
        const normalizedStatus = (selected.authorization_status || '').toUpperCase();
        if (normalizedStatus && normalizedStatus !== 'EFFECTIVE') {
          setAutoBindingStatus({
            variant: 'warning',
            message: `GMV Max exclusive authorization is ${normalizedStatus.toLowerCase()} for advertiser ${nextAdvertiserId}.`,
          });
          return;
        }
        setAutoBindingStatus({
          variant: 'success',
          message: `Confirmed GMV Max exclusive authorization for advertiser ${nextAdvertiserId}.`,
        });
        queryClient.invalidateQueries({ queryKey: ['gmvMax', 'config', workspaceId, provider, authId] });
      } catch (error) {
        if (cancelled) return;
        setAutoBindingStatus({
          variant: 'error',
          message: `Failed to check GMV Max exclusive authorization: ${formatError(error)}`,
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    advertiserId,
    authId,
    autoBindingMutation.isError,
    autoBindingMutation.isPending,
    autoBindingMutation.mutateAsync,
    businessCenterId,
    queryClient,
    provider,
    scopeOptionsReady,
    storeId,
    storeToAdvertiserId,
    workspaceId,
  ]);

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

  const canSync = Boolean(
    isScopeReady &&
      autoBindingVerified &&
      !autoBindingMutation.isPending &&
      !bindingConfigLoading &&
      !bindingConfigFetching &&
      !syncMutation.isPending &&
      !isSyncPolling,
  );
  const isSyncing = syncMutation.isPending || isSyncPolling;
  const canCreateSeries = Boolean(isScopeReady);

  const handleSyncMetadata = useCallback(async () => {
    if (!authId) {
      setMetaSyncError('Select a store before syncing metadata.');
      setMetaSyncMessage('');
      return;
    }
    setMetaSyncError(null);
    setMetaSyncMessage('');
    try {
      const response = await metadataSyncMutation.mutateAsync({ scope: 'meta', mode: 'full' });
      const summaryText = formatMetaSummary(response?.summary);
      const timestamp = new Date().toLocaleString();
      const runDetails = [];
      if (response?.run_id) runDetails.push(`run ${response.run_id}`);
      if (response?.task_id) runDetails.push(`task ${response.task_id}`);
      const suffix = runDetails.length ? ` (${runDetails.join(', ')})` : '';
      const nextMessage = summaryText
        ? `Metadata sync enqueued at ${timestamp}${suffix}. ${summaryText}`
        : `Metadata sync enqueued at ${timestamp}${suffix}.`;
      setMetaSyncMessage(nextMessage);
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
    } catch (error) {
      console.error('Failed to sync TikTok Business metadata', error);
      const message = formatError(error);
      setMetaSyncError(message || 'Metadata sync failed. Please try again.');
    }
  }, [
    accountsQuery,
    accountsQueryKey,
    authId,
    metadataSyncMutation,
    queryClient,
    scopeOptionsQuery,
    scopeOptionsQueryKey,
  ]);

  const handleSyncProducts = useCallback(async () => {
    if (!isScopeReady) {
      setProductSyncError('Select a store before syncing products.');
      setProductSyncMessage('');
      return;
    }
    if (!autoBindingVerified) {
      setProductSyncError('Wait for auto binding verification before syncing GMV Max products.');
      setProductSyncMessage('');
      return;
    }
    setProductSyncError(null);
    setProductSyncMessage('');
    try {
      const response = await productSyncMutation.mutateAsync({
        scope: 'products',
        mode: 'full',
        bc_id: businessCenterId ? String(businessCenterId) : undefined,
        advertiser_id: advertiserId ? String(advertiserId) : undefined,
        store_id: storeId ? String(storeId) : undefined,
        product_eligibility: 'gmv_max',
      });
      const timestamp = new Date().toLocaleString();
      const runParts = [];
      if (response?.run_id) runParts.push(`run ${response.run_id}`);
      if (response?.task_id) runParts.push(`task ${response.task_id}`);
      const suffix = runParts.length ? ` (${runParts.join(', ')})` : '';
      setProductSyncMessage(`Product sync enqueued at ${timestamp}${suffix}.`);
      await queryClient.invalidateQueries({
        queryKey: ['gmvMax', 'products', workspaceId, provider, authId],
      });
    } catch (error) {
      console.error('Failed to sync GMV Max products', error);
      const message = formatError(error);
      setProductSyncError(message || 'Product sync failed. Please try again.');
    }
  }, [
    advertiserId,
    authId,
    autoBindingVerified,
    businessCenterId,
    isScopeReady,
    productSyncMutation,
    provider,
    queryClient,
    storeId,
    workspaceId,
  ]);

  const handleSync = useCallback(async () => {
    if (!isScopeReady) {
      setSyncError('Please select a store before syncing GMV Max campaigns.');
      return;
    }
    if (bindingConfigLoading || bindingConfigFetching) {
      setSyncError('Binding configuration is still loading. Please wait before syncing.');
      return;
    }
    if (!autoBindingVerified) {
      setSyncError('Auto binding must finish verification before syncing GMV Max campaigns.');
      return;
    }
    setSyncError(null);
    setIsSyncPolling(true);
    const range = getRecentDateRange(7);
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
        start_date: range.start,
        end_date: range.end,
        metrics: DEFAULT_REPORT_METRICS,
        dimensions: ['campaign_id', 'stat_time_day'],
        enable_total_metrics: true,
      },
    };
    try {
      const response = await syncMutation.mutateAsync(payload);
      const taskId = response?.task_id || response?.taskId;
      if (!taskId) {
        throw new Error('Sync task was not enqueued.');
      }

      const maxAttempts = 90;
      const delayMs = 2000;
      for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        const status = await getGmvMaxSyncStatus(workspaceId, provider, authId, taskId);
        const state = status?.state || '';
        if (['SUCCESS', 'FAILURE', 'REVOKED'].includes(state)) {
          if (state === 'SUCCESS') {
            await refreshScopeQueries();
            setSyncError(null);
          } else {
            const message = formatError(status?.error) || 'Sync failed. Please try again.';
            setSyncError(message);
          }
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      }
      setSyncError('Sync is taking longer than expected. Please check the task status later.');
    } catch (error) {
      console.error('Failed to sync GMV Max campaigns', error);
      const message = formatError(error);
      setSyncError(
        typeof message === 'string' && message.trim().startsWith('[')
          ? 'Sync failed. Please try again.'
          : message,
      );
    } finally {
      setIsSyncPolling(false);
    }
  }, [
    advertiserId,
    bindingConfigFetching,
    bindingConfigLoading,
    businessCenterId,
    autoBindingVerified,
    isScopeReady,
    provider,
    refreshScopeQueries,
    storeId,
    syncMutation,
    workspaceId,
  ]);

  const handleOpenCreate = useCallback(() => {
    if (!canCreateSeries) return;
    setCreateModalOpen(true);
  }, [canCreateSeries]);

  const handleCloseCreate = useCallback(() => {
    setCreateModalOpen(false);
  }, []);

  const handleSeriesCreated = useCallback(() => {
    setCreateModalOpen(false);
    setSelectedProductIds([]);
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
      return params.toString() ? `?${params.toString()}` : '';
    },
    [advertiserId, authId, businessCenterId, provider, storeId],
  );

  const handleManage = useCallback(
    (campaignId) => {
      const search = buildCampaignSearchParams('automation');
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

  return (
    <div className="gmvmax-page">
      <header className="gmvmax-page__header">
        <div>
          <h1>GMV Max Overview</h1>
          <p className="gmvmax-page__subtitle">
            Monitor TikTok Business performance and manage GMV Max series.
          </p>
        </div>
        <span className="gmvmax-provider-badge">Provider: {PROVIDER_LABEL}</span>
      </header>

      <section className="gmvmax-card gmvmax-card--filters">
        <header className="gmvmax-card__header">
          <div>
            <h2>Scope filters</h2>
            <p>Select a store for GMV Max management. Business center and advertiser are auto-detected and hidden.</p>
          </div>
          <div className="gmvmax-card__actions">
            <button
              type="button"
              className="gmvmax-button gmvmax-button--ghost"
              onClick={handleSyncMetadata}
              disabled={!authId || metadataSyncMutation.isPending}
            >
              {metadataSyncMutation.isPending ? 'Syncing metadata…' : 'Sync account metadata'}
            </button>
            <button
              type="button"
              className="gmvmax-button gmvmax-button--ghost"
              onClick={handleSyncProducts}
              disabled={
                !isScopeReady ||
                !autoBindingVerified ||
                autoBindingMutation.isPending ||
                productSyncMutation.isPending
              }
            >
              {productSyncMutation.isPending ? 'Syncing GMV Max products…' : 'Sync GMV Max products'}
            </button>
          </div>
        </header>
        <div className="gmvmax-card__body">
          {metaSyncError || metaSyncMessage ? (
            <div
              className={`gmvmax-status-banner ${
                metaSyncError ? 'gmvmax-status-banner--error' : 'gmvmax-status-banner--success'
              }`}
            >
              {metaSyncError || metaSyncMessage}
            </div>
          ) : null}
          {productSyncError || productSyncMessage ? (
            <div
              className={`gmvmax-status-banner ${
                productSyncError ? 'gmvmax-status-banner--error' : 'gmvmax-status-banner--success'
              }`}
            >
              {productSyncError || productSyncMessage}
            </div>
          ) : null}
          {autoBindingStatus ? (
            <div
              className={`gmvmax-status-banner ${
                autoBindingStatus.variant
                  ? `gmvmax-status-banner--${autoBindingStatus.variant}`
                  : 'gmvmax-status-banner--muted'
              }`}
            >
              {autoBindingStatus.message}
            </div>
          ) : null}
          <div className="gmvmax-field-grid">
            <FormField label="Store">
              <select
                value={storeId}
                onChange={handleStoreChange}
                disabled={storeOptions.length === 0}
              >
                <option value="">Select store</option>
                {storeOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </FormField>
          </div>
          <div className="gmvmax-field-grid">
            <FormField label={`Scope presets (max ${MAX_SCOPE_PRESETS})`}>
              <div className="gmvmax-presets-row">
                <select value={selectedPresetId} onChange={handlePresetChange}>
                  <option value="">Select preset</option>
                  {scopePresets.map((preset) => (
                    <option key={preset.id} value={preset.id}>
                      {preset.label}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="gmvmax-button"
                  onClick={handleDeletePreset}
                  disabled={!selectedPresetId}
                >
                  Delete
                </button>
              </div>
            </FormField>
            <FormField label="Save current scope as preset">
              <div className="gmvmax-presets-row">
                <input
                  type="text"
                  value={presetLabelInput}
                  onChange={(event) => setPresetLabelInput(event.target.value)}
                  placeholder={defaultPresetLabel || 'Preset label'}
                  disabled={!isScopeReady}
                />
                <button
                  type="button"
                  className="gmvmax-button"
                  onClick={handleSavePreset}
                  disabled={!isScopeReady}
                >
                  Save preset
                </button>
              </div>
            </FormField>
          </div>
          <div className={scopeStatusClassName}>{scopeStatus.message}</div>
          <div className="gmvmax-card__footer">
            <button
              type="button"
              className="gmvmax-button gmvmax-button--primary"
              onClick={handleSync}
              disabled={!canSync || isSyncing}
            >
              {isSyncing ? 'Syncing…' : 'Sync GMV Max Campaigns'}
            </button>
            {syncError ? <p className="gmvmax-inline-error-text">{syncError}</p> : null}
          </div>
        </div>
      </section>

      <section className="gmvmax-card">
        <header className="gmvmax-card__header">
          <div>
            <h2>Unassigned products</h2>
            {selectedAccountLabel ? <p className="gmvmax-subtext">Account: {selectedAccountLabel}</p> : null}
          </div>
          <button
            type="button"
            className="gmvmax-button gmvmax-button--primary"
            onClick={handleOpenCreate}
            disabled={!canCreateSeries || unassignedProducts.length === 0}
          >
            Create GMV Max Series
          </button>
        </header>
        <div className="gmvmax-card__body">
          {!authId ? <p className="gmvmax-placeholder">Select a store to view products.</p> : null}
          {authId && !storeId ? (
            <p className="gmvmax-placeholder">Select a store to load products.</p>
          ) : null}
          {isScopeReady ? (
            <>
              <ProductSelectionPanel
                products={unassignedProducts}
                selectedIds={selectedProductIdSet}
                onToggle={handleToggleProduct}
                onToggleAll={handleToggleAllProducts}
                storeNames={storeNameById}
                loading={productsLoading}
                emptyMessage={
                  productsLoading
                    ? 'Loading products…'
                    : 'All products are currently assigned to a GMV Max series.'
                }
              />
              <p className="gmvmax-subtext">
                Selected {selectedProductIdSet.size} product(s) ready for a new GMV Max series.
              </p>
            </>
          ) : null}
          <ErrorBlock
            error={isScopeReady ? productsQuery.error : null}
            onRetry={productsQuery.refetch}
          />
        </div>
      </section>

      <section className="gmvmax-card">
        <header className="gmvmax-card__header">
          <h2>GMV Max series</h2>
        </header>
        <div className="gmvmax-card__body">
          <SeriesErrorNotice
            error={campaignsQueryEnabled ? campaignsQuery.error : null}
            onRetry={campaignsQueryEnabled ? campaignsQuery.refetch : undefined}
          />
          {campaignsLoading ? <Loading text="Loading campaigns…" /> : null}
          {!isScopeReady ? (
            <p className="gmvmax-placeholder">Complete the scope filters to load GMV Max series.</p>
          ) : null}
          {campaignsBlockedMessage ? (
            <p className="gmvmax-placeholder">{campaignsBlockedMessage}</p>
          ) : null}
          {campaignsQueryEnabled &&
          !campaignsLoading &&
          !campaignsQuery.error &&
          filteredCampaignCards.length === 0 ? (
            <p className="gmvmax-placeholder">No GMV Max series found for the selected scope.</p>
          ) : null}
          {campaignsQueryEnabled ? (
            <div className="gmvmax-campaign-grid">
              {filteredCampaignCards.map(({
                campaign,
                detail,
                detailLoading,
                detailError,
                detailRefetch,
              }) => (
                <CampaignCard
                  key={campaign.campaign_id || campaign.id}
                  campaign={campaign}
                  detail={detail}
                  detailLoading={detailLoading}
                  detailError={detailError}
                  onRetryDetail={detailRefetch}
                  workspaceId={workspaceId}
                  provider={provider}
                  authId={authId}
                  storeId={storeId}
                  onEdit={handleEditRequest}
                  onManage={handleManage}
                  onDashboard={handleDashboard}
                  products={products}
                />
              ))}
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
        products={unassignedProducts}
        productsLoading={productsLoading}
        storeNameById={storeNameById}
        initialProductIds={selectedProductIds}
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
