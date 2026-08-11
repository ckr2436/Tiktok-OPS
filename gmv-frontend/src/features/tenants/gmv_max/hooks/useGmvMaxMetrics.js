import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";

import { getGmvMaxMetrics } from "../api/gmvMaxApi.js";
import { composeMetricsQueryBaseKey, composeMetricsQueryKey } from "./gmvMaxQueries.js";
import { GmvMaxMetricsLevel } from "../constants/metrics.js";

const resolveEnabled = (flag) => Boolean(flag);

export function useGmvMaxMetrics({
  workspaceId,
  provider,
  authId,
  campaignId,
  advertiserId,
  metricsParams,
  campaignFilterId,
  itemGroupId,
  itemGroupIds,
  enabled = true,
  campaignEnabled = true,
  productEnabled = true,
  creativeEnabled = true,
  refetchInterval = 60 * 1000,
}) {
  const baseKey = useMemo(
    () => composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId),
    [authId, campaignId, provider, workspaceId],
  );

  const commonParams = useMemo(
    () => ({
      ...metricsParams,
      advertiser_id: advertiserId || undefined,
      campaign_id: campaignFilterId || undefined,
    }),
    [advertiserId, campaignFilterId, metricsParams],
  );

  const campaignParams = useMemo(
    () => ({ ...commonParams, level: GmvMaxMetricsLevel.CAMPAIGN }),
    [commonParams],
  );

  const productParams = useMemo(
    () => ({ ...commonParams, level: GmvMaxMetricsLevel.PRODUCT }),
    [commonParams],
  );

  const creativeParams = useMemo(
    () => ({
      ...commonParams,
      level: GmvMaxMetricsLevel.CREATIVE,
      item_group_ids:
        Array.isArray(itemGroupIds) && itemGroupIds.length > 0
          ? itemGroupIds
          : itemGroupId
            ? [itemGroupId]
            : undefined,
    }),
    [commonParams, itemGroupId, itemGroupIds],
  );
  const hasItemGroupFilter = Boolean(
    itemGroupId || (Array.isArray(itemGroupIds) && itemGroupIds.length > 0),
  );

  const queries = useQueries({
    queries: [
      {
        queryKey: composeMetricsQueryKey(workspaceId, provider, authId, campaignId, campaignParams),
        queryFn: ({ signal }) =>
          getGmvMaxMetrics(workspaceId, provider, authId, campaignId, campaignParams, { signal }),
        enabled: resolveEnabled(enabled && campaignEnabled && workspaceId && provider && authId && campaignId),
        retry: false,
        refetchInterval,
        refetchOnWindowFocus: true,
        refetchOnReconnect: true,
        staleTime: 30 * 1000,
      },
      {
        queryKey: composeMetricsQueryKey(workspaceId, provider, authId, campaignId, productParams),
        queryFn: ({ signal }) =>
          getGmvMaxMetrics(workspaceId, provider, authId, campaignId, productParams, { signal }),
        enabled: resolveEnabled(enabled && productEnabled && workspaceId && provider && authId && campaignId),
        retry: false,
        refetchInterval,
        refetchOnWindowFocus: true,
        refetchOnReconnect: true,
        staleTime: 30 * 1000,
      },
      {
        queryKey: composeMetricsQueryKey(workspaceId, provider, authId, campaignId, creativeParams),
        queryFn: ({ signal }) =>
          getGmvMaxMetrics(workspaceId, provider, authId, campaignId, creativeParams, { signal }),
        enabled: resolveEnabled(
          enabled && creativeEnabled && hasItemGroupFilter && workspaceId && provider && authId && campaignId,
        ),
        retry: false,
        refetchInterval,
        refetchOnWindowFocus: true,
        refetchOnReconnect: true,
        staleTime: 30 * 1000,
      },
    ],
  });

  return {
    baseKey,
    campaignMetrics: queries[0],
    productMetrics: queries[1],
    creativeMetrics: queries[2],
  };
}
