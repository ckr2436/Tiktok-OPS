import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";

import { getGmvMaxMetrics } from "../api/gmvMaxApi.js";
import { composeMetricsQueryBaseKey, composeMetricsQueryKey } from "./gmvMaxQueries.js";

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
  enabled = true,
  campaignEnabled = true,
  productEnabled = true,
  creativeEnabled = true,
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
    () => ({ ...commonParams, level: "campaign" }),
    [commonParams],
  );

  const productParams = useMemo(
    () => ({ ...commonParams, level: "product" }),
    [commonParams],
  );

  const creativeParams = useMemo(
    () => ({ ...commonParams, level: "creative", item_group_id: itemGroupId || undefined }),
    [commonParams, itemGroupId],
  );

  const queries = useQueries({
    queries: [
      {
        queryKey: composeMetricsQueryKey(workspaceId, provider, authId, campaignId, campaignParams),
        queryFn: () => getGmvMaxMetrics(workspaceId, provider, authId, campaignId, campaignParams),
        enabled: resolveEnabled(enabled && campaignEnabled && workspaceId && provider && authId && campaignId),
        retry: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        staleTime: 30 * 1000,
      },
      {
        queryKey: composeMetricsQueryKey(workspaceId, provider, authId, campaignId, productParams),
        queryFn: () => getGmvMaxMetrics(workspaceId, provider, authId, campaignId, productParams),
        enabled: resolveEnabled(enabled && productEnabled && workspaceId && provider && authId && campaignId),
        retry: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        staleTime: 30 * 1000,
      },
      {
        queryKey: composeMetricsQueryKey(workspaceId, provider, authId, campaignId, creativeParams),
        queryFn: () => getGmvMaxMetrics(workspaceId, provider, authId, campaignId, creativeParams),
        enabled: resolveEnabled(
          enabled && creativeEnabled && itemGroupId && workspaceId && provider && authId && campaignId,
        ),
        retry: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
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

