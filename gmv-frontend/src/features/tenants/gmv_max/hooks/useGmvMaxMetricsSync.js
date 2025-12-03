import { useCallback, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { syncGmvMaxMetrics } from "../api/gmvMaxApi.js";
import { composeMetricsQueryBaseKey } from "./gmvMaxQueries.js";
import { composeGmvTaskQueryKey } from "../utils/taskQueryKey.js";
import { isActiveTaskState, isTerminalTaskState, normalizeTaskState } from "../utils/taskState.js";
import { useGmvTaskPolling } from "./useGmvTaskPolling.js";

function createSyncError(message) {
  return new Error(message || "GMV Max 数据同步失败，请稍后重试。");
}

export function useGmvMaxMetricsSync({ workspaceId, provider, authId, campaignId }) {
  const queryClient = useQueryClient();
  const [currentTaskId, setCurrentTaskId] = useState();
  const [task, setTask] = useState(null);
  const [taskError, setTaskError] = useState(null);

  const handleSuccess = useCallback(
    (nextTask) => {
      const normalizedState = normalizeTaskState(nextTask?.state || "SUCCESS");
      setTask(nextTask ? { ...nextTask, state: normalizedState } : null);
      setTaskError(null);
      setCurrentTaskId(undefined);
      const metricsQueryKey = composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId);
      queryClient.invalidateQueries({ queryKey: metricsQueryKey, exact: false });
      queryClient.refetchQueries({ queryKey: metricsQueryKey, exact: false, type: "active" });
    },
    [authId, campaignId, provider, queryClient, workspaceId],
  );

  const handleFailure = useCallback((nextTask) => {
    const normalizedState = normalizeTaskState(nextTask?.state || "FAILURE");
    setTask(nextTask ? { ...nextTask, state: normalizedState } : null);
    setCurrentTaskId(undefined);
    const message = nextTask?.error || nextTask?.message;
    setTaskError(createSyncError(message));
  }, []);

  const syncMutation = useMutation({
    mutationFn: (payload) => syncGmvMaxMetrics(workspaceId, provider, authId, campaignId, payload),
    onMutate: () => {
      setTaskError(null);
      setTask(null);
      setCurrentTaskId(undefined);
      queryClient.removeQueries({
        queryKey: composeGmvTaskQueryKey(workspaceId, provider, authId),
        exact: false,
      });
    },
    onSuccess: (response) => {
      const nextTaskId = response?.task_id || response?.taskId;
      const nextState = normalizeTaskState(response?.state || "PENDING");
      const nextTask = { ...response, task_id: nextTaskId, state: nextState };

      setTask(nextTask);

      if (nextTaskId && isActiveTaskState(nextState)) {
        setCurrentTaskId(nextTaskId);
        return;
      }

      if (isTerminalTaskState(nextState)) {
        if (nextState === "SUCCESS") {
          handleSuccess(nextTask);
        } else {
          handleFailure(nextTask);
        }
        return;
      }

      if (!nextTaskId) {
        handleFailure({ ...nextTask, error: "GMV Max 同步任务创建失败：缺少 taskId" });
      }
    },
    onError: (error) => {
      setTaskError(error instanceof Error ? error : createSyncError());
      setCurrentTaskId(undefined);
    },
  });

  const { data: polledTask, isFetching: isPolling } = useGmvTaskPolling({
    taskId: currentTaskId,
    tenantId: workspaceId,
    provider,
    authId,
    onSuccess: handleSuccess,
    onFailure: handleFailure,
  });

  const isSyncing = useMemo(() => {
    if (syncMutation.isPending) return true;
    const state = normalizeTaskState(polledTask?.state || task?.state);
    return Boolean(currentTaskId && isActiveTaskState(state));
  }, [currentTaskId, polledTask?.state, syncMutation.isPending, task?.state]);

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
    isPolling: Boolean(currentTaskId && isPolling),
    syncState: polledTask?.state || task?.state || (syncMutation.isPending ? "PENDING" : undefined),
    syncError: taskError || syncMutation.error,
    task: polledTask || task,
  };
}
