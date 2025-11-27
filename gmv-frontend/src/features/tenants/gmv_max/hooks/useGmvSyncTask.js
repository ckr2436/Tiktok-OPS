import { useCallback, useMemo, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';

import { startGmvMaxSync, getTaskStatus } from '../api/gmvMaxApi.js';
import { formatError } from '../pages/gmvMaxOverview/helpers.js';

const POLL_INTERVAL_MS = 5000;
const MAX_ATTEMPTS = 120; // ~10 minutes

export function useGmvSyncTask({ workspaceId, provider, authId }) {
  const [lastTaskId, setLastTaskId] = useState(null);
  const [lastState, setLastState] = useState(null);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);
  const [error, setError] = useState(null);
  const inflightRef = useRef(false);

  const startMutation = useMutation({
    mutationFn: (payload) =>
      startGmvMaxSync(workspaceId, payload, {
        params: { provider, auth_id: authId },
      }),
  });

  const runSync = useCallback(
    async (payload) => {
      if (!workspaceId || !provider || !authId) {
        throw new Error('缺少同步所需的账户信息。');
      }
      if (inflightRef.current) {
        return { state: 'RUNNING', taskId: lastTaskId };
      }
      inflightRef.current = true;
      setError(null);
      try {
        const response = await startMutation.mutateAsync(payload);
        const taskId = response?.task_id || response?.taskId;
        const initialState = String(response?.state || '').toUpperCase();
        if (!taskId) {
          throw new Error('同步任务未被创建。');
        }
        setLastTaskId(taskId);
        setLastState(initialState || 'PENDING');

        if (['SUCCESS', 'FAILURE', 'REVOKED'].includes(initialState)) {
          if (initialState === 'SUCCESS') {
            setLastSyncedAt(Date.now());
          }
          if (initialState === 'SUCCESS') {
            return { state: initialState, taskId };
          }
          const message = formatError(response?.error) || '同步失败，请稍后再试。';
          throw new Error(message);
        }

        let attempt = 0;
        // eslint-disable-next-line no-constant-condition
        while (true) {
          // eslint-disable-next-line no-await-in-loop
          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
          // eslint-disable-next-line no-await-in-loop
          const status = await getTaskStatus(workspaceId, taskId, {
            params: { provider, auth_id: authId },
          });
          const state = String(status?.state || '').toUpperCase();
          setLastState(state);
          attempt += 1;
          if (['SUCCESS', 'FAILURE', 'REVOKED'].includes(state)) {
            if (state === 'SUCCESS') {
              setLastSyncedAt(Date.now());
              return { state, taskId };
            }
            const message = formatError(status?.error) || '同步失败，请稍后再试。';
            throw new Error(message);
          }
          if (attempt >= MAX_ATTEMPTS) {
            throw new Error('同步时间较长，请稍后查看任务状态。');
          }
        }
      } finally {
        inflightRef.current = false;
      }
    },
    [authId, lastTaskId, provider, startMutation, workspaceId],
  );

  return useMemo(
    () => ({
      runSync,
      isSyncing: inflightRef.current || startMutation.isPending,
      lastTaskId,
      lastState,
      lastSyncedAt,
      error,
    }),
    [error, lastState, lastSyncedAt, lastTaskId, runSync, startMutation.isPending],
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
      const result = await syncTask.runSync(payload);
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
