import { useMutation, useQuery } from '@tanstack/react-query';
import {
  applyGmvMaxAction,
  createGmvMaxCampaign,
  listGmvMaxCampaignCreatives,
  listGmvMaxCreativeHeating,
  listGmvMaxCreativeMetrics,
  getGmvMaxCampaign,
  getGmvMaxConfig,
  getGmvMaxMetrics,
  getGmvMaxOptions,
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
  syncGmvMaxCampaigns,
  syncGmvMaxMetrics,
  updateGmvMaxConfig,
  updateGmvMaxCampaign,
  updateGmvMaxStrategy,
} from '../api/gmvMaxApi.js';

function composeKey(...parts) {
  return ['gmvMax', ...parts];
}

function resolveEnabled(defaultEnabled, extra) {
  const normalized = extra ?? true;
  if (defaultEnabled === false || normalized === false) {
    return false;
  }
  return Boolean(defaultEnabled && normalized);
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
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('metrics', workspaceId, provider, authId, campaignId, params),
    queryFn: () => getGmvMaxMetrics(workspaceId, provider, authId, campaignId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
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
    queryKey: composeKey('campaign-creatives', workspaceId, provider, authId, campaignId, params),
    queryFn: () => listGmvMaxCampaignCreatives(workspaceId, provider, authId, campaignId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
    ...rest,
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
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('creative-metrics', workspaceId, provider, authId, campaignId, params),
    queryFn: () => listGmvMaxCreativeMetrics(workspaceId, provider, authId, campaignId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
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
  const { enabled, ...rest } = options;
  return useQuery({
    queryKey: composeKey('creative-heating', workspaceId, provider, authId, campaignId, params),
    queryFn: () => listGmvMaxCreativeHeating(workspaceId, provider, authId, campaignId, params),
    enabled: resolveEnabled(Boolean(workspaceId && provider && authId && campaignId), enabled),
    ...rest,
  });
}

export function useSyncGmvMaxCampaignsMutation(workspaceId, provider, authId, options = {}) {
  return useMutation({
    mutationFn: (payload) => syncGmvMaxCampaigns(workspaceId, provider, authId, payload),
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
