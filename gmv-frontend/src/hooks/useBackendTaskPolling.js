import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import http from "@/lib/http.js";

export const TERMINAL_STATES = ["SUCCESS", "FAILURE", "REVOKED"];

export function normalizeTaskState(value) {
  return String(value || "").toUpperCase();
}

function extractErrorMessage(error) {
  if (!error) return "任务查询失败或已过期，请重试同步";
  if (typeof error === "string") return error;
  if (error?.response?.data?.error?.message) return error.response.data.error.message;
  if (error?.response?.data?.message) return error.response.data.message;
  if (error?.response?.data?.detail) {
    const detail = error.response.data.detail;
    if (typeof detail === "string") return detail;
    try {
      return JSON.stringify(detail);
    } catch (serializationError) {
      return String(detail);
    }
  }
  if (error?.message) return error.message;
  return "任务查询失败或已过期，请重试同步";
}

export function useBackendTaskPolling({
  statusUrl,
  onSuccess,
  onFailure,
  intervalMs = 2000,
  clearStatusUrl,
}) {
  const effectiveStatusUrl = useMemo(() => {
    if (!statusUrl) return null;
    const normalized = String(statusUrl).trim();
    return normalized || null;
  }, [statusUrl]);

  const taskQuery = useQuery({
    queryKey: ["backend-task", effectiveStatusUrl],
    enabled: Boolean(effectiveStatusUrl),
    queryFn: () =>
      effectiveStatusUrl
        ? http.get(effectiveStatusUrl).then((response) => response?.data ?? response ?? null)
        : null,
    refetchInterval: (data) => {
      if (!effectiveStatusUrl) return false;
      const state = normalizeTaskState(data?.state || data?.status);
      if (!state) return intervalMs;
      if (!TERMINAL_STATES.includes(state)) return intervalMs;
      return false;
    },
    select: (data) => (data ? { ...data, state: normalizeTaskState(data.state || data.status) } : null),
    onSuccess: (data) => {
      if (!data) return;
      if (!TERMINAL_STATES.includes(data.state)) return;
      clearStatusUrl?.();
      if (data.state === "SUCCESS") {
        onSuccess?.(data);
      } else {
        const message = data.error || "任务执行失败";
        onFailure?.(data, typeof message === "string" ? message : undefined);
      }
    },
    onError: (error) => {
      clearStatusUrl?.();
      const status = error?.response?.status;
      const message = status === 404 ? "任务查询失败或已过期，请重试同步" : extractErrorMessage(error);
      onFailure?.(null, message);
    },
    retry: false,
  });

  return {
    task: taskQuery.data,
    isPolling: Boolean(effectiveStatusUrl && taskQuery.isFetching),
    error: taskQuery.error,
  };
}
