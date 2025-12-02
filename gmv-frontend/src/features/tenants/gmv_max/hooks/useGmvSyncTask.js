import { useCallback, useMemo, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';

import http from '@/lib/http.js';

import { startGmvMaxSync, getTaskStatus } from '../api/gmvMaxApi.js';
import { formatError } from '../utils/errors.js';
import { isTaskInProgress } from '../utils/taskPolling.js';

const POLL_INTERVAL_MS = 3000;
const MAX_POLL_ATTEMPTS = 40; // ~2 minutes

async function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function useGmvSyncTask({ workspaceId, provider, authId, onSuccess, onFailure }) {
  const [lastTaskId, setLastTaskId] = useState(null);
  const [lastState, setLastState] = useState(null);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);
  const [error, setError] = useState(null);
  const [syncing, setSyncing] = useState(false);

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
    async (statusUrl, taskId, attempt = 0) => {
      try {
        const status = await fetchTaskStatus(statusUrl, taskId);
        const state = String(status?.state || status?.status || '').toUpperCase();
        setLastState(state || 'PENDING');

        if (!isTaskInProgress(state)) {
          return { ...status, state };
        }

        if (attempt >= MAX_POLL_ATTEMPTS) {
          throw new Error('同步任务超时，请稍后重试。');
        }

        await wait(POLL_INTERVAL_MS);
        return pollStatus(statusUrl, taskId, attempt + 1);
      } catch (pollError) {
        throw pollError;
      }
    },
    [fetchTaskStatus],
  );

  const startSync = useCallback(
    async (payload) => {
      if (!workspaceId || !provider || !authId) {
        throw new Error('缺少同步所需的账户信息。');
      }

      setError(null);
      setSyncing(true);

      try {
        const response = await startMutation.mutateAsync(payload);
        const taskId = response?.task_id || response?.taskId;
        const statusUrl = response?.status_url || response?.statusUrl;
        const initialState = String(response?.state || '').toUpperCase();

        if (!taskId) {
          throw new Error('同步任务未被创建。');
        }

        setLastTaskId(taskId);
        setLastState(initialState || 'PENDING');

        if (!isTaskInProgress(initialState)) {
          if (initialState === 'SUCCESS') {
            setLastSyncedAt(Date.now());
            onSuccess?.();
          } else {
            const message = formatError(response?.error) || '同步失败，请稍后再试。';
            throw new Error(message);
          }

          return { state: initialState || 'PENDING', taskId };
        }

        const status = await pollStatus(statusUrl, taskId);
        const finalState = String(status?.state || '').toUpperCase();

        if (finalState === 'SUCCESS') {
          setLastSyncedAt(Date.now());
          onSuccess?.();
          return { state: finalState, taskId };
        }

        const message = formatError(status?.error) || '同步失败，请稍后再试。';
        throw new Error(message);
      } catch (syncError) {
        setError(syncError);
        onFailure?.(syncError);
        throw syncError;
      } finally {
        setSyncing(false);
      }
    },
    [authId, onFailure, onSuccess, pollStatus, provider, startMutation, workspaceId],
  );

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
