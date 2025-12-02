import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import http from '@/lib/http.js';

import { composeMetricsQueryBaseKey } from './gmvMaxQueries.js';
import { startGmvMaxSync } from '../api/gmvMaxApi.js';
import { formatError } from '../utils/errors.js';

const STORAGE_PREFIX = 'gmvmax:syncTask';

const TERMINAL_STATES = ['SUCCESS', 'FAILURE', 'REVOKED'];

function normalizeState(value) {
  return String(value || '').toUpperCase();
}

function isTerminalState(state) {
  if (!state) return false;
  return TERMINAL_STATES.includes(state);
}

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

  const handleTerminalState = useCallback(
    async (task) => {
      const normalizedState = normalizeState(task?.state || task?.status);
      setLastState(normalizedState || null);
      setStatusUrl(null);

      if (normalizedState === 'SUCCESS') {
        setError(null);
        setLastSyncedAt(Date.now());
        await queryClient.invalidateQueries({
          queryKey: composeMetricsQueryBaseKey(workspaceId, provider, authId, 'all'),
        });
        onSuccess?.(task);
        return;
      }

      const message = formatError(task?.error) || '同步失败，请稍后再试。';
      const syncError = new Error(message);
      setError(syncError);
      onFailure?.(syncError);
    },
    [onFailure, onSuccess, provider, queryClient, workspaceId],
  );

  const startSync = useCallback(
    async (payload) => {
      if (!workspaceId || !provider || !authId) {
        throw new Error('缺少同步所需的账户信息。');
      }

      setError(null);

      try {
        const response = await startMutation.mutateAsync(payload);
        const taskId = response?.task_id || response?.taskId;
        const nextStatusUrl = response?.status_url || response?.statusUrl;
        const initialState = normalizeState(response?.state || 'PENDING');

        if (!taskId && !nextStatusUrl) {
          throw new Error('同步任务未被创建。');
        }

        setLastTaskId(taskId || null);
        setLastState(initialState || null);
        setStatusUrl(nextStatusUrl || null);

        if (isTerminalState(initialState)) {
          await handleTerminalState({ ...response, state: initialState });
        }

        return { state: initialState, taskId };
      } catch (syncError) {
        setError(syncError);
        onFailure?.(syncError);
        throw syncError;
      }
    },
    [authId, handleTerminalState, onFailure, provider, startMutation, workspaceId],
  );

  const {
    data: task,
    isFetching: isPolling,
  } = useQuery({
    queryKey: ['gmvmax-sync-task', statusUrl],
    queryFn: () => {
      if (!statusUrl) return Promise.resolve(null);
      return http.get(statusUrl).then((response) => response?.data ?? response ?? null);
    },
    enabled: Boolean(statusUrl),
    refetchInterval: (data) => {
      if (!data) return 2000;
      const state = normalizeState(data?.state || data?.status);
      if (!isTerminalState(state)) return 2000;
      return false;
    },
    onSuccess: (data) => {
      if (!data) return;
      const state = normalizeState(data?.state || data?.status);
      setLastState(state || null);
      if (isTerminalState(state)) {
        handleTerminalState({ ...data, state });
      }
    },
    onError: (pollError) => {
      setLastState('FAILURE');
      setStatusUrl(null);
      setError(pollError);
      onFailure?.(pollError);
    },
    retry: false,
  });

  const isSyncing = useMemo(() => {
    const state = normalizeState(task?.state || task?.status || lastState);
    if (statusUrl && !isTerminalState(state)) return true;
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
