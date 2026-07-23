import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { startGmvMaxSync, syncGmvMaxMetrics } from "../api/gmvMaxApi.js";
import { formatError } from "../utils/errors.js";
import { composeGmvTaskQueryKey } from "../utils/taskQueryKey.js";
import { isActiveTaskState, isTerminalTaskState, normalizeTaskState } from "../utils/taskState.js";
import { useGmvTaskPolling } from "./useGmvTaskPolling.js";

function normalizeTaskId(taskId) {
  if (taskId === undefined || taskId === null) return null;
  const normalized = String(taskId).trim();
  return normalized === "" ? null : normalized;
}

function createSyncError(error) {
  const message = formatError(error) || "同步失败，请稍后再试。";
  return new Error(message);
}

export function useGmvSyncTask({
  workspaceId,
  provider,
  authId,
  campaignId,
  onSuccess,
  onFailure,
}) {
  const queryClient = useQueryClient();
  const [currentTaskId, setCurrentTaskId] = useState();
  const [lastTaskId, setLastTaskId] = useState(null);
  const [lastState, setLastState] = useState(null);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);
  const [syncStartedAt, setSyncStartedAt] = useState(null);
  const [error, setError] = useState(null);
  const completionResolversRef = useRef([]);
  const earlyTerminalTasksRef = useRef(new Map());

  const resolveCompletion = useCallback((task, isError = false) => {
    const resolvedTaskId = normalizeTaskId(task?.task_id || task?.taskId);
    const matchers = completionResolversRef.current;
    completionResolversRef.current = [];
    let matched = false;
    matchers.forEach(({ taskId, resolve, reject }) => {
      const normalizedTaskId = normalizeTaskId(taskId);
      if (normalizedTaskId && resolvedTaskId && normalizedTaskId !== resolvedTaskId) {
        completionResolversRef.current.push({ taskId: normalizedTaskId, resolve, reject });
        return;
      }
      matched = true;
      if (isError) {
        reject?.(task);
      } else {
        resolve?.(task);
      }
    });
    // A very fast task may finish between mutateAsync's onSuccess callback and
    // startSync registering its waiter.  Retain that terminal result briefly
    // so the caller gets a settled promise instead of waiting forever.
    if (!matched && resolvedTaskId) {
      earlyTerminalTasksRef.current.set(resolvedTaskId, { task, isError });
    }
  }, []);

  const handleSuccess = useCallback(
    async (task) => {
      const normalizedState = normalizeTaskState(task?.state || "SUCCESS");
      const resolvedTaskId =
        normalizeTaskId(task?.task_id || task?.taskId || currentTaskId || lastTaskId) || null;
      setLastTaskId(resolvedTaskId);
      setLastState(normalizedState);
      setCurrentTaskId(undefined);
      setSyncStartedAt(null);
      setError(null);
      setLastSyncedAt(Date.now());
      await queryClient.invalidateQueries({
        queryKey: ["gmvMax", "metrics", workspaceId, provider, authId],
        exact: false,
      });
      await queryClient.refetchQueries({
        queryKey: ["gmvMax", "metrics", workspaceId, provider, authId],
        exact: false,
        type: "all",
      });
      await queryClient.invalidateQueries({
        queryKey: ["gmvMax", "campaign", workspaceId, provider, authId],
        exact: false,
      });
      await queryClient.refetchQueries({
        queryKey: ["gmvMax", "campaign", workspaceId, provider, authId],
        exact: false,
        type: "all",
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
      const resolvedTaskId =
        normalizeTaskId(task?.task_id || task?.taskId || currentTaskId || lastTaskId) || null;
      setLastTaskId(resolvedTaskId);
      setLastState(normalizedState);
      setCurrentTaskId(undefined);
      setSyncStartedAt(null);
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
    const normalizedState = normalizeTaskState(polledTask.state);
    if (
      polledTask.task_id &&
      !isActiveTaskState(normalizedState) &&
      completionResolversRef.current.length > 0
    ) {
      resolveCompletion(
        {
          ...polledTask,
          task_id: polledTask.task_id,
          state: normalizedState,
        },
        normalizedState !== "SUCCESS",
      );
    }
  }, [polledTask, resolveCompletion]);

  const startMutation = useMutation({
    mutationFn: (payload) =>
      campaignId
        ? syncGmvMaxMetrics(workspaceId, provider, authId, campaignId, payload)
        : startGmvMaxSync(workspaceId, provider, authId, payload),
    onMutate: () => {
      setError(null);
      setLastState(null);
      setLastTaskId(null);
      setCurrentTaskId(undefined);
      setSyncStartedAt(Date.now());
      earlyTerminalTasksRef.current.clear();
      queryClient.removeQueries({
        queryKey: composeGmvTaskQueryKey(workspaceId, provider, authId),
        exact: false,
      });
    },
    onSuccess: async (response) => {
      const taskId = normalizeTaskId(response?.task_id || response?.taskId);
      const state = normalizeTaskState(response?.state || "PENDING");
      const nextTask = { ...response, task_id: taskId, state };

      setLastTaskId(taskId || null);
      setLastState(state);

      if (taskId && isActiveTaskState(state)) {
        setCurrentTaskId(taskId);
        setSyncStartedAt(Date.now());
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
      const taskId = normalizeTaskId(response?.task_id || response?.taskId);

      if (isTerminalTaskState(normalizedState)) {
        if (taskId) earlyTerminalTasksRef.current.delete(taskId);
        return {
          state: normalizedState,
          taskId,
          completion:
            normalizedState === "SUCCESS"
              ? Promise.resolve({ ...response, state: normalizedState, task_id: taskId })
              : null,
        };
      }

      if (!taskId || !isActiveTaskState(normalizedState)) {
        // A non-terminal response without an identity cannot be polled or
        // proven fresh.  Do not create an unresolvable completion promise.
        return {
          state: normalizedState,
          taskId: null,
          completion: null,
        };
      }

      const earlyTerminal = earlyTerminalTasksRef.current.get(taskId);
      if (earlyTerminal) {
        earlyTerminalTasksRef.current.delete(taskId);
        return {
          state: normalizedState,
          taskId,
          // Resolve with the terminal task even on failure.  Callers already
          // inspect its state, and a pre-rejected promise could become an
          // unhandled rejection when a manual sync caller ignores it.
          completion: Promise.resolve(earlyTerminal.task),
        };
      }

      const completionPromise = new Promise((resolve, reject) => {
        completionResolversRef.current.push({ taskId, resolve, reject });
      });
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
  campaignId,
  reportParams,
  levels = ["OVERVIEW"],
  campaignIds,
  itemGroupIds,
  freshnessMs = 10 * 60 * 1000,
}) {
  const syncTask = useGmvSyncTask({ workspaceId, provider, authId, campaignId });
  const lastEnsuredRef = useRef(0);

  const normalizedCampaignIds = useMemo(() => {
    if (!campaignIds) return [];
    const list = Array.isArray(campaignIds) ? campaignIds : [campaignIds];
    return list
      .map((value) => (value === undefined || value === null ? "" : String(value)))
      .filter(Boolean);
  }, [campaignIds]);

  const normalizedItemGroupIds = useMemo(() => {
    if (!itemGroupIds) return [];
    const list = Array.isArray(itemGroupIds) ? itemGroupIds : [itemGroupIds];
    return list
      .map((value) => (value === undefined || value === null ? "" : String(value)))
      .filter(Boolean);
  }, [itemGroupIds]);

  const ensureFresh = useCallback(
    async () => {
      const now = Date.now();
      if (lastEnsuredRef.current && now - lastEnsuredRef.current < freshnessMs) {
        return true;
      }
      if (!workspaceId || !provider || !authId) {
        return false;
      }
      const payload = {
        start_date: reportParams?.start_date || reportParams?.startDate || null,
        end_date: reportParams?.end_date || reportParams?.endDate || null,
        levels,
        campaign_ids: normalizedCampaignIds.length ? normalizedCampaignIds : undefined,
        item_group_ids: normalizedItemGroupIds.length ? normalizedItemGroupIds : undefined,
      };
      let result;
      try {
        result = await syncTask.startSync(payload);
      } catch (startError) {
        // Mutating actions must fail closed when their preflight sync cannot
        // even be started.  The sync hook already exposes the underlying
        // error for the UI; this boolean prevents the caller from proceeding.
        // eslint-disable-next-line no-console
        console.error("GMV Max sync preflight could not start", startError);
        return false;
      }
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
        // startSync returns no result when another sync is already pending.
        // That is not proof of freshness; callers must wait and retry instead
        // of recording a false successful preflight.
        return false;
      }
      return false;
    },
    [
      freshnessMs,
      reportParams,
      syncTask,
      workspaceId,
      provider,
      authId,
      levels,
      normalizedCampaignIds,
      normalizedItemGroupIds,
    ],
  );

  return {
    ensureFresh,
    isSyncing: syncTask.isSyncing,
    lastSyncedAt: syncTask.lastSyncedAt,
    error: syncTask.error,
  };
}
