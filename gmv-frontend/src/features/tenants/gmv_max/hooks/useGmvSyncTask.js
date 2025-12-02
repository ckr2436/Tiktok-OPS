import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';

import http from '@/lib/http.js';

import { startGmvMaxSync, getTaskStatus } from '../api/gmvMaxApi.js';
import { formatError } from '../utils/errors.js';
import { isTaskInProgress } from '../utils/taskPolling.js';

const POLL_INTERVAL_MS = 3000;
const MAX_POLL_ATTEMPTS = 40; // ~2 minutes

const normalizeState = (value) => String(value || '').toUpperCase();

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
  const [lastTaskId, setLastTaskId] = useState(null);
  const [lastState, setLastState] = useState(null);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);
  const [error, setError] = useState(null);
  const [syncing, setSyncing] = useState(false);

  const timerRef = useRef(null);
  const taskRef = useRef(null);

  const storageKey = useMemo(() => getStorageKey(workspaceId, provider, authId), [authId, provider, workspaceId]);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const finalizeSuccess = useCallback(
    (state) => {
      clearTimer();
      setSyncing(false);
      taskRef.current = null;
      persistTask(storageKey, null);
      setLastState(state || 'SUCCESS');
      setLastSyncedAt(Date.now());
      onSuccess?.();
    },
    [clearTimer, onSuccess, storageKey],
  );

  const finalizeFailure = useCallback(
    (syncError, state) => {
      clearTimer();
      setSyncing(false);
      taskRef.current = null;
      persistTask(storageKey, null);
      if (state) {
        setLastState(state);
      }
      setError(syncError);
      onFailure?.(syncError);
    },
    [clearTimer, onFailure, storageKey],
  );

  const startMutation = useMutation({
    mutationFn: (payload) =>
      startGmvMaxSync(workspaceId, payload, {
        params: { provider, auth_id: authId },
      }),
  });

  const fetchTaskStatus = useCallback(
    async (statusUrl, taskId) => {
      if (statusUrl) {
        const response = await http.get(statusUrl);
        return response?.data ?? response ?? {};
      }
      const status = await getTaskStatus(workspaceId, taskId, {
        params: { provider, auth_id: authId },
      });
      return status;
    },
    [authId, provider, workspaceId],
  );

  const pollStatus = useCallback(
    (statusUrl, taskId, attempt = 0) => {
      let settled = false;
      const pollOnce = async (currentAttempt, resolve, reject) => {
        try {
          const status = await fetchTaskStatus(statusUrl, taskId);
          const state = normalizeState(status?.state || status?.status || 'PENDING');
          setLastState(state || 'PENDING');

          if (!isTaskInProgress(state)) {
            settled = true;
            clearTimer();
            resolve({ ...status, state });
            return;
          }

          if (currentAttempt >= MAX_POLL_ATTEMPTS) {
            settled = true;
            clearTimer();
            reject(new Error('GMV Max 数据同步超时，请稍后重试。'));
            return;
          }

          clearTimer();
          timerRef.current = window.setTimeout(
            () => pollOnce(currentAttempt + 1, resolve, reject),
            POLL_INTERVAL_MS,
          );
        } catch (pollError) {
          if (settled) return;
          settled = true;
          clearTimer();
          reject(pollError);
        }
      };

      return new Promise((resolve, reject) => {
        pollOnce(attempt, resolve, reject);
      });
    },
    [clearTimer, fetchTaskStatus],
  );

  const startSync = useCallback(
    async (payload) => {
      if (!workspaceId || !provider || !authId) {
        throw new Error('缺少同步所需的账户信息。');
      }

      setError(null);
      setSyncing(true);
      clearTimer();

      try {
        const response = await startMutation.mutateAsync(payload);
        const taskId = response?.task_id || response?.taskId;
        const statusUrl = response?.status_url || response?.statusUrl;
        const initialState = normalizeState(response?.state || 'PENDING');

        if (!taskId && !statusUrl) {
          throw new Error('同步任务未被创建。');
        }

        setLastTaskId(taskId || null);
        setLastState(initialState || 'PENDING');
        taskRef.current = { taskId, statusUrl };
        persistTask(storageKey, { taskId, statusUrl });

        if (!isTaskInProgress(initialState)) {
          const normalizedState = initialState || 'PENDING';
          if (normalizedState === 'SUCCESS') {
            finalizeSuccess(normalizedState);
            return { state: normalizedState, taskId };
          }
          const message = formatError(response?.error) || '同步失败，请稍后再试。';
          const syncError = new Error(message);
          finalizeFailure(syncError, normalizedState);
          throw syncError;
        }

        const status = await pollStatus(statusUrl, taskId);
        const finalState = normalizeState(status?.state || '');

        if (finalState === 'SUCCESS') {
          finalizeSuccess(finalState);
          return { state: finalState, taskId };
        }

        const message = formatError(status?.error) || '同步失败，请稍后再试。';
        const syncError = new Error(message);
        finalizeFailure(syncError, finalState);
        throw syncError;
      } catch (syncError) {
        finalizeFailure(syncError);
        throw syncError;
      }
    },
    [authId, finalizeFailure, finalizeSuccess, pollStatus, provider, startMutation, storageKey, workspaceId],
  );

  useEffect(() => {
    if (!storageKey) return undefined;
    const persisted = restoreTask(storageKey);
    if (!persisted?.taskId && !persisted?.statusUrl) return undefined;

    setSyncing(true);
    setError(null);
    taskRef.current = { ...persisted };
    setLastTaskId(persisted.taskId || null);
    setLastState('PENDING');

    pollStatus(persisted.statusUrl, persisted.taskId)
      .then((status) => {
        const finalState = normalizeState(status?.state || '');
        if (finalState === 'SUCCESS') {
          finalizeSuccess(finalState);
          return;
        }
        const message = formatError(status?.error) || '同步失败，请稍后再试。';
        const syncError = new Error(message);
        finalizeFailure(syncError, finalState);
      })
      .catch((syncError) => {
        finalizeFailure(syncError);
      });

    return () => {
      clearTimer();
    };
  }, [finalizeFailure, finalizeSuccess, pollStatus, storageKey, clearTimer]);

  useEffect(() => () => clearTimer(), [clearTimer]);

  return useMemo(
    () => ({
      startSync,
      isSyncing: syncing || startMutation.isPending,
      lastTaskId,
      lastState,
      lastSyncedAt,
      error,
    }),
    [error, lastState, lastSyncedAt, lastTaskId, startMutation.isPending, startSync, syncing],
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
