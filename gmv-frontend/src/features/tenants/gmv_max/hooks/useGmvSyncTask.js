import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { startGmvMaxSync } from "../api/gmvMaxApi.js";
import { formatError } from "../utils/errors.js";
import { composeGmvTaskQueryKey } from "../utils/taskQueryKey.js";
import { isActiveTaskState, isTerminalTaskState, normalizeTaskState } from "../utils/taskState.js";
import { useGmvTaskPolling } from "./useGmvTaskPolling.js";

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
  const completionResolversRef = useRef([]);

  const resolveCompletion = useCallback((task, isError = false) => {
    const resolvedTaskId = task?.task_id || task?.taskId || null;
    const matchers = completionResolversRef.current;
    completionResolversRef.current = [];
    matchers.forEach(({ taskId, resolve, reject }) => {
      if (taskId && resolvedTaskId && taskId !== resolvedTaskId) {
        completionResolversRef.current.push({ taskId, resolve, reject });
        return;
      }
      if (isError) {
        reject?.(task);
      } else {
        resolve?.(task);
      }
    });
  }, []);

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
        queryKey: ["gmvMax", "metrics", workspaceId, provider, authId],
        exact: false,
      });
      await queryClient.invalidateQueries({
        queryKey: ["gmvMax", "campaigns", workspaceId, provider, authId],
        exact: false,
      });
      onSuccess?.(task);
      resolveCompletion({ ...task, task_id: resolvedTaskId });
    },
    [authId, currentTaskId, lastTaskId, onSuccess, provider, queryClient, resolveCompletion, workspaceId],
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
      resolveCompletion({ ...task, task_id: resolvedTaskId }, true);
    },
    [currentTaskId, lastTaskId, onFailure, resolveCompletion],
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
        queryKey: composeGmvTaskQueryKey(workspaceId, provider, authId),
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
      const normalizedState = normalizeTaskState(response?.state || "PENDING");
      const taskId = response?.task_id || response?.taskId;
      const completionPromise = new Promise((resolve, reject) => {
        completionResolversRef.current.push({ taskId, resolve, reject });
      });

      if (isTerminalTaskState(normalizedState)) {
        if (normalizedState === "SUCCESS") {
          resolveCompletion({ ...response, state: normalizedState, task_id: taskId });
        } else {
          resolveCompletion({ ...response, state: normalizedState, task_id: taskId }, true);
        }
      }

      return {
        state: normalizedState,
        taskId,
        completion: completionPromise,
      };
    },
    [authId, isSyncing, onFailure, provider, resolveCompletion, startMutation, workspaceId],
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
      if (result?.completion) {
        try {
          const completed = await result.completion;
          if (normalizeTaskState(completed?.state) === 'SUCCESS') {
            lastEnsuredRef.current = Date.now();
            return true;
          }
          return false;
        } catch (completionError) {
          // eslint-disable-next-line no-console
          console.error("GMV Max sync preflight failed", completionError);
          return false;
        }
      }
      if (result?.taskId) {
        return false;
      }
      if (!result) {
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
