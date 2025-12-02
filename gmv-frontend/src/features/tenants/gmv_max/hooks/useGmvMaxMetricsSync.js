import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getGmvMaxTaskStatus, syncGmvMaxMetrics } from '../api/gmvMaxApi.js';
import { composeMetricsQueryBaseKey } from './gmvMaxQueries.js';
import { isTaskInProgress } from '../utils/taskPolling.js';

function normalizeState(value) {
  return String(value || '').toUpperCase();
}

export function useGmvMaxMetricsSync({ workspaceId, provider, authId, campaignId }) {
  const queryClient = useQueryClient();
  const [statusUrl, setStatusUrl] = useState(null);
  const [taskId, setTaskId] = useState(null);
  const [taskError, setTaskError] = useState(null);

  const syncMutation = useMutation({
    mutationFn: (payload) => syncGmvMaxMetrics(workspaceId, provider, authId, campaignId, payload),
    onSuccess: (response) => {
      setStatusUrl(response?.status_url || response?.statusUrl || null);
      setTaskId(response?.task_id || response?.taskId || null);
    },
    onError: () => {
      setStatusUrl(null);
      setTaskId(null);
    },
  });

  const taskKey = useMemo(
    () => ['gmvMax', 'metrics-sync-task', workspaceId, provider, authId, campaignId, statusUrl || taskId],
    [authId, campaignId, provider, statusUrl, taskId, workspaceId],
  );

  const taskQuery = useQuery({
    queryKey: taskKey,
    enabled: Boolean(statusUrl || taskId),
    queryFn: () => getGmvMaxTaskStatus(workspaceId, provider, authId, statusUrl || taskId),
    refetchInterval: (data) => {
      const state = normalizeState(data?.state);
      if (!state || isTaskInProgress(state)) return 2000;
      return false;
    },
  });

  useEffect(() => {
    if (taskQuery.isError) {
      setStatusUrl(null);
      setTaskId(null);
    }
  }, [taskQuery.isError]);

  useEffect(() => {
    const state = normalizeState(taskQuery.data?.state);
    if (!state) return;

    if (state === 'SUCCESS') {
      setTaskError(null);
      queryClient.invalidateQueries({
        queryKey: composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId),
        exact: false,
      });
    }

    if (state === 'SUCCESS' || state === 'FAILURE' || state === 'REVOKED') {
      if (state !== 'SUCCESS') {
        setTaskError(new Error('GMV Max 数据同步失败，请稍后重试。'));
      }
      setStatusUrl(null);
      setTaskId(null);
    }
  }, [campaignId, provider, authId, workspaceId, queryClient, taskQuery.data?.state]);

  useEffect(() => {
    setStatusUrl(null);
    setTaskId(null);
    setTaskError(null);
  }, [campaignId, provider, authId, workspaceId]);

  const isSyncing = useMemo(() => {
    if (syncMutation.isPending) return true;
    if (!statusUrl && !taskId) return false;
    const state = normalizeState(taskQuery.data?.state || 'PENDING');
    return isTaskInProgress(state) && !taskQuery.isError;
  }, [statusUrl, syncMutation.isPending, taskId, taskQuery.data?.state, taskQuery.isError]);

  return {
    startSync: (payload) => syncMutation.mutate(payload),
    startSyncAsync: (payload) => syncMutation.mutateAsync(payload),
    isSyncing,
    syncState: taskQuery.data?.state || (syncMutation.isPending ? 'PENDING' : undefined),
    syncError: taskError || syncMutation.error || taskQuery.error,
  };
}
