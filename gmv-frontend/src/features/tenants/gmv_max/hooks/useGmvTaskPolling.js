import axios from "axios";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getGmvMaxTaskStatus } from "../api/gmvMaxApi.js";
import { isActiveTaskState, normalizeTaskState } from "../utils/taskState.js";

const POLLING_INTERVAL_MS = 2000;

export function useGmvTaskPolling({ taskId, tenantId, provider, authId, onSuccess, onFailure }) {
  const queryClient = useQueryClient();
  const normalizedTaskId = taskId ? String(taskId).trim() : "";

  const queryKey = ["gmvmax-task", tenantId, provider, authId, normalizedTaskId || undefined];

  return useQuery({
    queryKey,
    enabled: Boolean(normalizedTaskId && tenantId && provider),
    queryFn: async () => {
      if (!normalizedTaskId) {
        throw new Error("missing taskId");
      }
      const data = await getGmvMaxTaskStatus(tenantId, provider, authId, normalizedTaskId);
      return { ...data, state: normalizeTaskState(data?.state) };
    },
    refetchInterval: (task) => {
      if (!task) return POLLING_INTERVAL_MS;
      return isActiveTaskState(task.state) ? POLLING_INTERVAL_MS : false;
    },
    retry: 2,
    onSuccess: (task) => {
      if (!task || isActiveTaskState(task.state)) return;
      if (task.state === "SUCCESS") {
        onSuccess?.(task);
      } else {
        onFailure?.(task);
      }
    },
    onError: (error) => {
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
    },
    select: (task) => (task ? { ...task, state: normalizeTaskState(task.state) } : task),
  });
}
