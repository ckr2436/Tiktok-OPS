import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { useBackendTaskPolling, normalizeTaskState, TERMINAL_STATES } from "@/hooks/useBackendTaskPolling.js";

import { composeMetricsQueryBaseKey } from "./gmvMaxQueries.js";
import { startGmvMaxSync } from "../api/gmvMaxApi.js";
import { formatError } from "../utils/errors.js";

const STORAGE_PREFIX = 'gmvmax:syncTask';

function getStorageKey(workspaceId, provider, authId) {
  if (!workspaceId || !provider || !authId) return '';
  return `${STORAGE_PREFIX}:${workspaceId}:${provider}:${authId}`;
}

function persistTask(storageKey, task) {
  if (!storageKey || typeof window === 'undefined') return;
  try {
    if (!task) {
      window.localStorage?.removeItem(storageKey);
      return;
    }
    window.localStorage?.setItem(storageKey, JSON.stringify(task));
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('Failed to persist GMV Max sync task', err);
  }
}

function restoreTask(storageKey) {
  if (!storageKey || typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage?.getItem(storageKey);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('Failed to restore GMV Max sync task', err);
    return null;
  }
}

export function useGmvSyncTask({ workspaceId, provider, authId, onSuccess, onFailure }) {
  const [statusUrl, setStatusUrl] = useState(null);
  const [lastTaskId, setLastTaskId] = useState(null);
  const [lastState, setLastState] = useState(null);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);
  const [error, setError] = useState(null);
  const clearStatusUrl = useCallback(() => {
    setStatusUrl(null);
    setLastTaskId(null);
  }, []);

  const resetTaskTracking = useCallback(() => {
    setLastState(null);
    clearStatusUrl();
  }, [clearStatusUrl]);

  const queryClient = useQueryClient();

  const storageKey = useMemo(() => getStorageKey(workspaceId, provider, authId), [authId, provider, workspaceId]);

  useEffect(() => {
    if (!storageKey) return;
    const persisted = restoreTask(storageKey);
    if (!persisted?.statusUrl) return;

    setStatusUrl(persisted.statusUrl);
    setLastTaskId(persisted.taskId || null);
    setLastState('PENDING');
  }, [storageKey]);

  useEffect(() => {
    persistTask(storageKey, statusUrl ? { taskId: lastTaskId, statusUrl } : null);
  }, [lastTaskId, statusUrl, storageKey]);

  const startMutation = useMutation({
    mutationFn: (payload) =>
      startGmvMaxSync(workspaceId, payload, {
        params: { provider, auth_id: authId },
      }),
  });

  const handleSuccess = useCallback(
    async (task) => {
      const normalizedState = normalizeTaskState(task?.state || task?.status);
      setLastTaskId(task?.task_id || task?.taskId || lastTaskId || null);
      setLastState(normalizedState || null);
      clearStatusUrl();
      setError(null);
      setLastSyncedAt(Date.now());
      await queryClient.invalidateQueries({
        queryKey: composeMetricsQueryBaseKey(workspaceId, provider, authId, "all"),
      });
      onSuccess?.(task);
    },
    [authId, clearStatusUrl, lastTaskId, onSuccess, provider, queryClient, workspaceId],
  );

  const handleFailure = useCallback(
    (task, message) => {
      const normalizedState = normalizeTaskState(task?.state || task?.status || "FAILURE");
      setLastTaskId(task?.task_id || task?.taskId || lastTaskId || null);
      setLastState(normalizedState || null);
      clearStatusUrl();
      const errorMessage = message || formatError(task?.error) || "同步失败，请稍后再试。";
      const syncError = new Error(errorMessage);
      setError(syncError);
      onFailure?.(syncError);
    },
    [clearStatusUrl, lastTaskId, onFailure],
  );

  const handleTerminalState = useCallback(
    async (task, message) => {
      const normalizedState = normalizeTaskState(task?.state || task?.status);
      if (normalizedState === "SUCCESS") {
        await handleSuccess({ ...task, state: normalizedState });
        return;
      }
      handleFailure(task ? { ...task, state: normalizedState } : null, message);
    },
    [handleFailure, handleSuccess],
  );

  const startSync = useCallback(
    async (payload) => {
      if (!workspaceId || !provider || !authId) {
        throw new Error('缺少同步所需的账户信息。');
      }

      setError(null);
      resetTaskTracking();

      try {
        const response = await startMutation.mutateAsync(payload);
        const taskId = response?.task_id || response?.taskId;
        const nextStatusUrl = response?.status_url || response?.statusUrl;
        const initialState = normalizeTaskState(response?.state || 'PENDING');

        if (!taskId && !nextStatusUrl) {
          throw new Error('同步任务未被创建。');
        }

        setLastTaskId(taskId || null);
        setLastState(initialState || null);
        if (nextStatusUrl && !TERMINAL_STATES.includes(initialState)) {
          setStatusUrl(nextStatusUrl);
        }

        if (TERMINAL_STATES.includes(initialState)) {
          await handleTerminalState({ ...response, state: initialState });
        }

        return { state: initialState, taskId };
      } catch (syncError) {
        setError(syncError);
        onFailure?.(syncError);
        throw syncError;
      }
    },
    [authId, handleTerminalState, onFailure, provider, resetTaskTracking, startMutation, workspaceId],
  );

  const { task, isPolling } = useBackendTaskPolling({
    statusUrl,
    intervalMs: 2000,
    clearStatusUrl,
    onSuccess: handleTerminalState,
    onFailure: (polledTask, message) => {
      handleFailure(polledTask, message);
    },
  });

  const isSyncing = useMemo(() => {
    const state = normalizeTaskState(task?.state || task?.status || lastState);
    if (statusUrl && state && !TERMINAL_STATES.includes(state)) return true;
    if (statusUrl && !state) return true;
    return false;
  }, [lastState, statusUrl, task]);

  const mutationPending = startMutation.isPending;

  return useMemo(
    () => ({
      startSync,
      isSyncing: isSyncing || mutationPending || isPolling,
      lastTaskId,
      lastState,
      lastSyncedAt,
      error,
    }),
    [error, isPolling, isSyncing, lastState, lastSyncedAt, lastTaskId, mutationPending, startSync],
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
    [freshnessMs, reportParams, storeId, syncTask],
  );

  return {
    ensureFresh,
    isSyncing: syncTask.isSyncing,
    lastSyncedAt: syncTask.lastSyncedAt,
    error: syncTask.error,
  };
}
