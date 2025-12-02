import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { composeMetricsQueryBaseKey } from "./gmvMaxQueries.js";
import { startGmvMaxSync } from "../api/gmvMaxApi.js";
import { formatError } from "../utils/errors.js";
import { isActiveTaskState, isTerminalTaskState, normalizeTaskState } from "../utils/taskState.js";
import { useGmvTaskPolling } from "./useGmvTaskPolling.js";

const TASK_QUERY_KEY = (workspaceId, provider, authId) => ["gmvmax-task", workspaceId, provider, authId];

function createSyncError(error) {
  const message = formatError(error) || "同步失败，请稍后再试。";
  return new Error(message);
}

export function useGmvSyncTask({ workspaceId, provider, authId, onSuccess, onFailure }) {
  const queryClient = useQueryClient();
  const [currentTaskId, setCurrentTaskId] = useState();
  const [lastTaskId, setLastTaskId] = useState(null);
  const [lastState, setLastState] = useState(null);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);
  const [error, setError] = useState(null);

  const handleSuccess = useCallback(
    async (task) => {
      const normalizedState = normalizeTaskState(task?.state || "SUCCESS");
      const resolvedTaskId = task?.task_id || task?.taskId || currentTaskId || lastTaskId || null;
      setLastTaskId(resolvedTaskId);
      setLastState(normalizedState);
      setCurrentTaskId(undefined);
      setError(null);
      setLastSyncedAt(Date.now());
      await queryClient.invalidateQueries({
        queryKey: composeMetricsQueryBaseKey(workspaceId, provider, authId, "all"),
        exact: false,
      });
      onSuccess?.(task);
    },
    [authId, currentTaskId, lastTaskId, onSuccess, provider, queryClient, workspaceId],
  );

  const handleFailure = useCallback(
    (task) => {
      const normalizedState = normalizeTaskState(task?.state || "FAILURE");
      const resolvedTaskId = task?.task_id || task?.taskId || currentTaskId || lastTaskId || null;
      setLastTaskId(resolvedTaskId);
      setLastState(normalizedState);
      setCurrentTaskId(undefined);
      const syncError = task instanceof Error ? task : createSyncError(task?.error || task?.message || task);
      setError(syncError);
      onFailure?.(syncError);
    },
    [currentTaskId, lastTaskId, onFailure],
  );

  const { data: polledTask, isFetching: isPolling } = useGmvTaskPolling({
    taskId: currentTaskId,
    tenantId: workspaceId,
    provider,
    authId,
    onSuccess: handleSuccess,
    onFailure: handleFailure,
  });

  useEffect(() => {
    if (!polledTask) return;
    if (polledTask.task_id) {
      setLastTaskId(polledTask.task_id);
    }
    if (polledTask.state) {
      setLastState(normalizeTaskState(polledTask.state));
    }
  }, [polledTask]);

  const startMutation = useMutation({
    mutationFn: (payload) =>
      startGmvMaxSync(workspaceId, payload, {
        params: { provider, auth_id: authId },
      }),
    onMutate: () => {
      setError(null);
      setLastState(null);
      setLastTaskId(null);
      setCurrentTaskId(undefined);
      queryClient.removeQueries({
        queryKey: TASK_QUERY_KEY(workspaceId, provider, authId),
        exact: false,
      });
    },
    onSuccess: async (response) => {
      const taskId = response?.task_id || response?.taskId;
      const state = normalizeTaskState(response?.state || "PENDING");
      const nextTask = { ...response, task_id: taskId, state };

      setLastTaskId(taskId || null);
      setLastState(state);

      if (taskId && isActiveTaskState(state)) {
        setCurrentTaskId(taskId);
        return;
      }

      if (isTerminalTaskState(state)) {
        if (state === "SUCCESS") {
          await handleSuccess(nextTask);
        } else {
          handleFailure(nextTask);
        }
        return;
      }

      if (!taskId) {
        handleFailure({ ...nextTask, error: "同步任务未返回 taskId" });
      }
    },
    onError: (mutationError) => {
      const syncError = mutationError instanceof Error ? mutationError : createSyncError(mutationError);
      setError(syncError);
      onFailure?.(syncError);
    },
  });

  const isSyncing = useMemo(() => {
    if (startMutation.isPending) return true;
    const state = normalizeTaskState(polledTask?.state || lastState);
    return Boolean(currentTaskId && isActiveTaskState(state));
  }, [currentTaskId, lastState, polledTask?.state, startMutation.isPending]);

  const startSync = useCallback(
    async (payload) => {
      if (!workspaceId || !provider || !authId) {
        const missingError = new Error("缺少同步所需的账户信息。");
        setError(missingError);
        onFailure?.(missingError);
        return;
      }
      if (startMutation.isPending || isSyncing) return;
      const response = await startMutation.mutateAsync(payload);
      return {
        state: normalizeTaskState(response?.state || "PENDING"),
        taskId: response?.task_id || response?.taskId,
      };
    },
    [authId, isSyncing, onFailure, provider, startMutation, workspaceId],
  );

  return useMemo(
    () => ({
      startSync,
      isSyncing: isSyncing || isPolling,
      lastTaskId,
      lastState,
      lastSyncedAt,
      error,
    }),
    [error, isPolling, isSyncing, lastState, lastSyncedAt, lastTaskId, startSync],
  );
}

export function useEnsureFreshGmvData({
  workspaceId,
  provider,
  authId,
  storeId,
  reportParams,
  freshnessMs = 10 * 60 * 1000,
}) {
  const syncTask = useGmvSyncTask({ workspaceId, provider, authId });
  const lastEnsuredRef = useRef(0);

  const ensureFresh = useCallback(
    async () => {
      const now = Date.now();
      if (lastEnsuredRef.current && now - lastEnsuredRef.current < freshnessMs) {
        return true;
      }
      if (!workspaceId || !provider || !authId) {
        return false;
      }
      const normalizedStoreId = storeId ? String(storeId) : undefined;
      const payload = { store_id: normalizedStoreId };
      if (reportParams) {
        payload.report = {
          ...reportParams,
          store_ids: reportParams.store_ids || (normalizedStoreId ? [normalizedStoreId] : undefined),
        };
      }
      const result = await syncTask.startSync(payload);
      if (result?.state === 'SUCCESS') {
        lastEnsuredRef.current = Date.now();
        return true;
      }
      return false;
    },
    [freshnessMs, reportParams, storeId, syncTask, workspaceId, provider, authId],
  );

  return {
    ensureFresh,
    isSyncing: syncTask.isSyncing,
    lastSyncedAt: syncTask.lastSyncedAt,
    error: syncTask.error,
  };
}
