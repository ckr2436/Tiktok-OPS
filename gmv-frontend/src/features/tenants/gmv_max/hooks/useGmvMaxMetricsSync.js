import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getGmvMaxTaskStatus, syncGmvMaxMetrics } from "../api/gmvMaxApi.js";
import { composeMetricsQueryBaseKey } from "./gmvMaxQueries.js";

const PENDING_STATES = new Set(["PENDING", "STARTED", "RETRY"]);
const TERMINAL_STATES = new Set(["SUCCESS", "FAILURE", "REVOKED"]);

function normalizeState(value) {
  return String(value || "").toUpperCase();
}

function isPending(state) {
  return PENDING_STATES.has(normalizeState(state));
}

function isTerminal(state) {
  return TERMINAL_STATES.has(normalizeState(state));
}

export function useGmvMaxMetricsSync({ workspaceId, provider, authId, campaignId }) {
  const queryClient = useQueryClient();
  const [statusUrl, setStatusUrl] = useState(null);
  const [task, setTask] = useState(null);
  const [taskError, setTaskError] = useState(null);

  const resetTask = useCallback(() => {
    setStatusUrl(null);
    setTask(null);
    setTaskError(null);
  }, []);

  const handleTerminalState = useCallback(
    (nextTask) => {
      const state = normalizeState(nextTask?.state);
      if (!state) return;

      setTask(nextTask || null);
      setStatusUrl(null);

      if (state === "SUCCESS") {
        setTaskError(null);
        queryClient.invalidateQueries({
          queryKey: composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId),
          exact: false,
        });
        return;
      }

      setTaskError(new Error("GMV Max 数据同步失败，请稍后重试。"));
    },
    [authId, campaignId, provider, queryClient, workspaceId],
  );

  const syncMutation = useMutation({
    mutationFn: (payload) => syncGmvMaxMetrics(workspaceId, provider, authId, campaignId, payload),
    onMutate: () => {
      setTaskError(null);
    },
    onSuccess: (response) => {
      const nextStatusUrl = response?.status_url || response?.statusUrl || null;
      const nextState = normalizeState(response?.state);

      setTask(response || null);

      if (nextStatusUrl && isPending(nextState)) {
        setStatusUrl(nextStatusUrl);
        return;
      }

      if (nextStatusUrl) {
        setStatusUrl(nextStatusUrl);
        return;
      }

      if (isTerminal(nextState)) {
        handleTerminalState({ ...response, state: nextState });
      }
    },
    onError: (error) => {
      setTaskError(error);
      setStatusUrl(null);
    },
  });

  const taskKey = useMemo(
    () => ["gmvMax", "metrics-sync-task", workspaceId, provider, authId, campaignId, statusUrl],
    [authId, campaignId, provider, statusUrl, workspaceId],
  );

  const taskQuery = useQuery({
    queryKey: taskKey,
    enabled: Boolean(workspaceId && provider && authId && campaignId && statusUrl),
    queryFn: () => getGmvMaxTaskStatus(workspaceId, provider, authId, statusUrl),
    refetchInterval: (data) => (isPending(data?.state) ? 2000 : false),
    retry: false,
    onSuccess: (data) => {
      if (!data) return;
      const state = normalizeState(data.state);
      if (isTerminal(state)) {
        handleTerminalState({ ...data, state });
      } else {
        setTask({ ...data, state });
      }
    },
    onError: (error) => {
      setTaskError(error);
      setStatusUrl(null);
    },
  });

  useEffect(() => {
    resetTask();
  }, [campaignId, provider, authId, workspaceId, resetTask]);

  const isSyncing = useMemo(() => {
    if (syncMutation.isPending) return true;
    if (!statusUrl) return false;
    const state = normalizeState(taskQuery.data?.state || task?.state);
    return isPending(state) && !taskQuery.isError;
  }, [statusUrl, syncMutation.isPending, taskQuery.data?.state, taskQuery.isError, task?.state]);

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
    isPolling: Boolean(statusUrl && (taskQuery.isFetching || isPending(taskQuery.data?.state))),
    syncState: taskQuery.data?.state || task?.state || (syncMutation.isPending ? "PENDING" : undefined),
    syncError: taskError || syncMutation.error || taskQuery.error,
    task: taskQuery.data || task,
  };
}
