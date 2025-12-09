import { useMutation, useQuery } from '@tanstack/react-query';
import { isCanceledRequest } from '../../../../lib/http.js';
import {
  applyGmvMaxAction,
  createGmvMaxCampaign,
  listGmvMaxCampaignCreatives,
  listGmvMaxCreativeHeating,
  listGmvMaxCreativeMetrics,
  getGmvMaxCampaign,
  getGmvMaxConfig,
  getGmvMaxMetrics,
  getGmvMaxBindingStatus,
  autoDiscoverGmvMaxBinding,
  rebindAutoGmvMaxBinding,
  getGmvMaxOptions,
  getGmvMaxIdentities,
  precheckGmvMaxCampaign,
  getGmvMaxStrategy,
  listAccounts,
  listAdvertisers,
  listBusinessCenters,
  listGmvMaxActionLogs,
  listGmvMaxCampaigns,
  startGmvMaxCreativeHeating,
  stopGmvMaxCreativeHeating,
  listProducts,
  listProviders,
  listStores,
  syncAccountMetadata,
  syncAccountProducts,
  previewGmvMaxStrategy,
  syncAdvertiserBalance,
  syncGmvMaxCampaigns,
  getGmvMaxSyncInterval,
  syncGmvMaxMetrics,
  updateGmvMaxSyncInterval,
  updateGmvMaxConfig,
  updateGmvMaxCampaign,
  updateGmvMaxStrategy,
  normalizeIdList,
} from '../api/gmvMaxApi.js';
import { GmvMaxMetricsLevel } from '../constants/metrics.js';

function composeKey(...parts) {
  return ['gmvMax', ...parts];
}

export function composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId) {
  return composeKey('metrics', workspaceId, provider, authId, campaignId || '');
}

function normalizeMetricsKeyParams(params = {}, campaignId) {
  const normalized = params ? { ...params } : {};
  const level = String(normalized.level || '').toLowerCase();
  const campaignIds = normalizeIdList(normalized.campaign_ids ?? normalized.campaign_id);
  const itemGroupIds = normalizeIdList(normalized.item_group_ids ?? normalized.item_group_id);

  const needsCampaignFilter = level === 'product' || level === 'creative';
  if (needsCampaignFilter && campaignIds.length === 0 && campaignId) {
    campaignIds.push(String(campaignId));
  }

  return {
    ...normalized,
    start_date: normalized.start_date || normalized.startDate || '',
    end_date: normalized.end_date || normalized.endDate || '',
    advertiser_id: normalized.advertiser_id ?? normalized.advertiserId ?? '',
    level,
    campaign_ids: campaignIds,
    item_group_ids: itemGroupIds,
  };
}

export function composeMetricsQueryKey(workspaceId, provider, authId, campaignId, params) {
  const normalizedParams = normalizeMetricsKeyParams(params, campaignId);
  return [...composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId), normalizedParams];
}

function resolveEnabled(defaultEnabled, extra) {
  const normalized = extra ?? true;
  if (defaultEnabled === false || normalized === false) {
    return false;
  }
  return Boolean(defaultEnabled && normalized);
}

function ignoreCanceledOnError(handler) {
  if (typeof handler !== 'function') return undefined;
  return (error) => {
    if (isCanceledRequest(error)) {
      return;
    }
    handler(error);
  };
}

export function useProvidersQuery(workspaceId, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('providers', workspaceId),
    queryFn: () => listProviders(workspaceId),
    enabled: resolveEnabled(Boolean(workspaceId), enabled),
    ...rest,
  });
}

export function useAccountsQuery(workspaceId, provider, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('accounts', workspaceId, provider, params),
    queryFn: () => listAccounts(workspaceId, provider, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider), enabled),
    ...rest,
  });
}

export function useBusinessCentersQuery(workspaceId, provider, authId, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('business-centers', workspaceId, provider, authId, params),
    queryFn: () => listBusinessCenters(workspaceId, provider, authId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useAdvertisersQuery(workspaceId, provider, authId, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('advertisers', workspaceId, provider, authId, params),
    queryFn: () => listAdvertisers(workspaceId, provider, authId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useStoresQuery(workspaceId, provider, authId, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('stores', workspaceId, provider, authId, params),
    queryFn: () => listStores(workspaceId, provider, authId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useProductsQuery(workspaceId, provider, authId, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('products', workspaceId, provider, authId, params),
    queryFn: () => listProducts(workspaceId, provider, authId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useGmvMaxIdentitiesQuery(workspaceId, provider, authId, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('identities', workspaceId, provider, authId, params),
    queryFn: () => getGmvMaxIdentities(workspaceId, provider, authId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useSyncAccountMetadataMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => syncAccountMetadata(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useSyncAccountProductsMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => syncAccountProducts(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useGmvMaxOptionsQuery(workspaceId, provider, authId, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('options', workspaceId, provider, authId, params),
    queryFn: () => getGmvMaxOptions(workspaceId, provider, authId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useGmvMaxConfigQuery(workspaceId, provider, authId, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('config', workspaceId, provider, authId),
    queryFn: () => getGmvMaxConfig(workspaceId, provider, authId),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useGmvMaxCampaignsQuery(workspaceId, provider, authId, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('campaigns', workspaceId, provider, authId, params),
    queryFn: () => listGmvMaxCampaigns(workspaceId, provider, authId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useGmvMaxBindingStatusQuery(workspaceId, provider, authId, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('binding-status', workspaceId, provider, authId, params),
    queryFn: () => getGmvMaxBindingStatus(workspaceId, provider, authId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useGmvMaxCampaignQuery(workspaceId, provider, authId, campaignId, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('campaign', workspaceId, provider, authId, campaignId),
    queryFn: () => getGmvMaxCampaign(workspaceId, provider, authId, campaignId),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
    ...rest,
  });
}

export function useGmvMaxMetricsQuery(workspaceId, provider, authId, campaignId, params = {}, options = {}) {
  const { enabled, refetchInterval, onError, ...rest } = options;
  const normalizedLevel = String(params?.level || '').toLowerCase();
  const allowsCampaignless = normalizedLevel === GmvMaxMetricsLevel.OVERVIEW;
  return useQuery({
    queryKey: composeMetricsQueryKey(workspaceId, provider, authId, campaignId, params),
    queryFn: () => getGmvMaxMetrics(workspaceId, provider, authId, campaignId, params),
    enabled: resolveEnabled(
      Boolean(workspaceId && provider && authId && (campaignId || allowsCampaignless)),
      enabled,
    ),
    refetchInterval,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: 30 * 1000,
    onError: ignoreCanceledOnError(onError),
    ...rest,
  });
}

export function useGmvMaxStrategyQuery(workspaceId, provider, authId, campaignId, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('strategy', workspaceId, provider, authId, campaignId),
    queryFn: () => getGmvMaxStrategy(workspaceId, provider, authId, campaignId),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
    ...rest,
  });
}

export function useGmvMaxActionLogsQuery(workspaceId, provider, authId, campaignId, params = {}, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('action-logs', workspaceId, provider, authId, campaignId, params),
    queryFn: () => listGmvMaxActionLogs(workspaceId, provider, authId, campaignId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
    ...rest,
  });
}

export function useGmvMaxCampaignCreativesQuery(
  workspaceId,
  provider,
  authId,
  campaignId,
  params = {},
  options = {},
) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeMetricsQueryKey(workspaceId, provider, authId, campaignId, params),
    queryFn: () => listGmvMaxCampaignCreatives(workspaceId, provider, authId, campaignId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
    ...rest,
  });
}

export function useGmvMaxAutoBindingMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => autoDiscoverGmvMaxBinding(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useGmvMaxRebindAutoMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => rebindAutoGmvMaxBinding(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useSyncAdvertiserBalanceMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => syncAdvertiserBalance(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useGmvMaxCreativeMetricsQuery(
  workspaceId,
  provider,
  authId,
  campaignId,
  params = {},
  options = {},
) {
  const { enabled, refetchInterval, onError, ...rest } = options;
  return useQuery({
    queryKey: composeMetricsQueryKey(workspaceId, provider, authId, campaignId, params),
    queryFn: () => listGmvMaxCreativeMetrics(workspaceId, provider, authId, campaignId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
    refetchInterval,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: 30 * 1000,
    onError: ignoreCanceledOnError(onError),
    ...rest,
  });
}

export function useGmvMaxCreativeHeatingQuery(
  workspaceId,
  provider,
  authId,
  campaignId,
  params = {},
  options = {},
) {
  const { enabled, refetchInterval, onError, ...rest } = options;
  return useQuery({
    queryKey: composeKey('creative-heating', workspaceId, provider, authId, campaignId, params),
    queryFn: () => listGmvMaxCreativeHeating(workspaceId, provider, authId, campaignId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
    refetchInterval,
    onError: ignoreCanceledOnError(onError),
    ...rest,
  });
}

export function useSyncGmvMaxCampaignsMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => syncGmvMaxCampaigns(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useGmvMaxSyncIntervalQuery(workspaceId, provider, authId, options = {}) {
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('sync-interval', workspaceId, provider, authId),
    queryFn: () => getGmvMaxSyncInterval(workspaceId, provider, authId),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId), enabled),
    ...rest,
  });
}

export function useUpdateGmvMaxSyncIntervalMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => updateGmvMaxSyncInterval(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useSyncGmvMaxMetricsMutation(workspaceId, provider, authId, campaignId, options = {}) {
  return useMutation({
    mutationFn: (payload) => syncGmvMaxMetrics(workspaceId, provider, authId, campaignId, payload),
    ...options,
  });
}

export function useApplyGmvMaxActionMutation(workspaceId, provider, authId, campaignId, options = {}) {
  return useMutation({
    mutationFn: (payload) => applyGmvMaxAction(workspaceId, provider, authId, campaignId, payload),
    ...options,
  });
}

export function useStartGmvMaxCreativeHeatingMutation(
  workspaceId,
  provider,
  authId,
  campaignId,
  options = {},
) {
  return useMutation({
    mutationFn: ({ creativeId, payload }) =>
      startGmvMaxCreativeHeating(workspaceId, provider, authId, campaignId, creativeId, payload),
    ...options,
  });
}

export function useStopGmvMaxCreativeHeatingMutation(
  workspaceId,
  provider,
  authId,
  campaignId,
  options = {},
) {
  return useMutation({
    mutationFn: ({ creativeId, payload }) =>
      stopGmvMaxCreativeHeating(workspaceId, provider, authId, campaignId, creativeId, payload),
    ...options,
  });
}

export function useUpdateGmvMaxConfigMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => updateGmvMaxConfig(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useUpdateGmvMaxStrategyMutation(workspaceId, provider, authId, campaignId, options = {}) {
  return useMutation({
    mutationFn: (payload) => updateGmvMaxStrategy(workspaceId, provider, authId, campaignId, payload),
    ...options,
  });
}

export function useCreateGmvMaxCampaignMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => createGmvMaxCampaign(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useGmvMaxPrecheckMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => precheckGmvMaxCampaign(workspaceId, provider, authId, payload),
    ...options,
  });
}

export function useUpdateGmvMaxCampaignMutation(
  workspaceId,
  provider,
  authId,
  campaignId,
  options = {},
) {
  return useMutation({
    mutationFn: (payload) => updateGmvMaxCampaign(workspaceId, provider, authId, campaignId, payload),
    ...options,
  });
}

export function usePreviewGmvMaxStrategyMutation(workspaceId, provider, authId, campaignId, options = {}) {
  return useMutation({
    mutationFn: (payload) => previewGmvMaxStrategy(workspaceId, provider, authId, campaignId, payload),
    ...options,
  });
}
