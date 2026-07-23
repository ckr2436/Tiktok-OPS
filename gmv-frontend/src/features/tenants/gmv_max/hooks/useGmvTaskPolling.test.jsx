import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/gmvMaxApi.js", () => ({
  getGmvMaxTaskStatus: vi.fn(),
}));

import { getGmvMaxTaskStatus } from "../api/gmvMaxApi.js";
import { useGmvTaskPolling } from "./useGmvTaskPolling.js";

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return function Wrapper({ children }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("useGmvTaskPolling", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("notifies success when a task reaches its terminal state", async () => {
    getGmvMaxTaskStatus.mockResolvedValue({
      task_id: "task-1",
      state: "SUCCESS",
      result: null,
      error: null,
    });
    const onSuccess = vi.fn();
    const onFailure = vi.fn();

    renderHook(
      () =>
        useGmvTaskPolling({
          taskId: "task-1",
          tenantId: 3,
          provider: "tiktok-business",
          authId: 3,
          onSuccess,
          onFailure,
        }),
      { wrapper: createWrapper() },
    );

    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(onFailure).not.toHaveBeenCalled();
    expect(onSuccess).toHaveBeenCalledWith(
      expect.objectContaining({ task_id: "task-1", state: "SUCCESS" }),
    );
  });
});
