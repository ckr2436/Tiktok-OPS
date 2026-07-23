import axios from "axios";
import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getGmvMaxTaskStatus } from "../api/gmvMaxApi.js";
import { composeGmvTaskQueryKey } from "../utils/taskQueryKey.js";
import { isActiveTaskState, normalizeTaskState } from "../utils/taskState.js";

const POLLING_INTERVAL_MS = 2000;

export function useGmvTaskPolling({ taskId, tenantId, provider, authId, onSuccess, onFailure }) {
  const queryClient = useQueryClient();
  const notifiedTerminalRef = useRef(null);
  const normalizedTaskId = taskId ? String(taskId).trim() : "";

  const queryKey = composeGmvTaskQueryKey(tenantId, provider, authId, normalizedTaskId || undefined);

  const query = useQuery({
    queryKey,
    enabled: Boolean(normalizedTaskId && tenantId && provider),
    queryFn: async () => {
      if (!normalizedTaskId) {
        throw new Error("missing taskId");
      }
      const data = await getGmvMaxTaskStatus(tenantId, provider, authId, normalizedTaskId);
      const state = normalizeTaskState(data?.state || data?.status);
      return { ...data, state };
    },
    refetchInterval: (query) => {
      const task = query?.state?.data;
      if (!task) return POLLING_INTERVAL_MS;
      return isActiveTaskState(task.state) ? POLLING_INTERVAL_MS : false;
    },
    refetchIntervalInBackground: true,
    retry: 2,
    select: (task) =>
      task ? { ...task, state: normalizeTaskState(task.state || task.status) } : task,
  });

  useEffect(() => {
    notifiedTerminalRef.current = null;
  }, [normalizedTaskId]);

  useEffect(() => {
    const task = query.data;
    if (!task || isActiveTaskState(task.state)) return;

    const notificationKey = `${normalizedTaskId}:${task.state}`;
    if (notifiedTerminalRef.current === notificationKey) return;
    notifiedTerminalRef.current = notificationKey;

    if (task.state === "SUCCESS") {
      onSuccess?.(task);
    } else {
      onFailure?.(task);
    }

    queryClient.removeQueries({ queryKey, exact: true });
  }, [normalizedTaskId, onFailure, onSuccess, query.data, queryClient, queryKey]);

  useEffect(() => {
    const error = query.error;
    if (!error || !normalizedTaskId) return;

    const notificationKey = `${normalizedTaskId}:REQUEST_ERROR`;
    if (notifiedTerminalRef.current === notificationKey) return;
    notifiedTerminalRef.current = notificationKey;

    const failure = {
      task_id: normalizedTaskId,
      state: "FAILURE",
      result: null,
      error: null,
    };

    if (axios.isAxiosError(error)) {
      failure.error = error.response?.status === 404 ? "TASK_NOT_FOUND" : error.message;
    } else if (error instanceof Error) {
      failure.error = error.message;
    }

    onFailure?.(failure);
    queryClient.removeQueries({ queryKey, exact: true });
  }, [normalizedTaskId, onFailure, query.error, queryClient, queryKey]);

  return query;
}
