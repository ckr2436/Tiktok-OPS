import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import {
  normalizeTaskState,
  useBackendTaskPolling,
} from "@/hooks/useBackendTaskPolling.js";

import { syncGmvMaxMetrics } from "../api/gmvMaxApi.js";
import { composeMetricsQueryBaseKey } from "./gmvMaxQueries.js";
import { isActiveTaskState, isTerminalTaskState } from "../utils/taskState.js";

export function useGmvMaxMetricsSync({ workspaceId, provider, authId, campaignId }) {
  const queryClient = useQueryClient();
  const [statusUrl, setStatusUrl] = useState(null);
  const [task, setTask] = useState(null);
  const [taskError, setTaskError] = useState(null);
  const clearStatusUrl = useCallback(() => setStatusUrl(null), []);

  const resetTask = useCallback(() => {
    setStatusUrl(null);
    setTask(null);
    setTaskError(null);
  }, []);

  const handleTerminalState = useCallback(
    (nextTask, message) => {
      const state = normalizeTaskState(nextTask?.state);
      if (!isTerminalTaskState(state)) return;

      setTask(nextTask || null);
      clearStatusUrl();

      if (state === "SUCCESS") {
        setTaskError(null);
        queryClient.invalidateQueries({
          queryKey: composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId),
          exact: false,
        });
        return;
      }

      setTaskError(new Error(message || "GMV Max 数据同步失败，请稍后重试。"));
    },
    [authId, campaignId, clearStatusUrl, provider, queryClient, workspaceId],
  );

  const syncMutation = useMutation({
    mutationFn: (payload) => syncGmvMaxMetrics(workspaceId, provider, authId, campaignId, payload),
    onMutate: () => {
      setTaskError(null);
    },
    onSuccess: (response) => {
      const nextStatusUrl = response?.status_url || response?.statusUrl || null;
      const nextState = normalizeTaskState(response?.state);

      setTask(response || null);

      if (nextStatusUrl && isActiveTaskState(nextState)) {
        setStatusUrl(nextStatusUrl);
        return;
      }

      if (nextStatusUrl && !isTerminalTaskState(nextState)) {
        setStatusUrl(nextStatusUrl);
      }

      if (isTerminalTaskState(nextState)) {
        handleTerminalState({ ...response, state: nextState });
      }
    },
    onError: (error) => {
      setTaskError(error);
      clearStatusUrl();
    },
  });

  const { task: polledTask, isPolling, error: pollingError } = useBackendTaskPolling({
    statusUrl,
    intervalMs: 2000,
    clearStatusUrl,
    onSuccess: (data) => handleTerminalState(data),
    onFailure: (data, message) => handleTerminalState(data || { state: "FAILURE" }, message),
  });

  useEffect(() => {
    resetTask();
  }, [campaignId, provider, authId, workspaceId, resetTask]);

  const isSyncing = useMemo(() => {
    if (syncMutation.isPending) return true;
    if (!statusUrl) return false;
    const state = normalizeTaskState(polledTask?.state || task?.state);
    return isActiveTaskState(state) && !pollingError;
  }, [pollingError, polledTask?.state, statusUrl, syncMutation.isPending, task?.state]);

  const startSync = useCallback(
    (payload) => {
      if (syncMutation.isPending || isSyncing) return;
      syncMutation.mutate(payload);
    },
    [isSyncing, syncMutation],
  );

  const startSyncAsync = useCallback(
    (payload) => {
      if (syncMutation.isPending || isSyncing) return Promise.resolve();
      return syncMutation.mutateAsync(payload);
    },
    [isSyncing, syncMutation],
  );

  return {
    startSync,
    startSyncAsync,
    isSyncing,
    isCreatingTask: syncMutation.isPending,
    isPolling: Boolean(statusUrl && (isPolling || isActiveTaskState(polledTask?.state))),
    syncState: polledTask?.state || task?.state || (syncMutation.isPending ? "PENDING" : undefined),
    syncError: taskError || syncMutation.error || pollingError,
    task: polledTask || task,
  };
}
