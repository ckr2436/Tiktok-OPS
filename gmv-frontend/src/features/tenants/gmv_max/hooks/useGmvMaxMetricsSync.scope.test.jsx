import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/gmvMaxApi.js', () => ({
  syncGmvMaxMetrics: vi.fn(),
}));

vi.mock('./useGmvTaskPolling.js', () => ({
  useGmvTaskPolling: () => ({ data: null, isFetching: false }),
}));

import { syncGmvMaxMetrics } from '../api/gmvMaxApi.js';
import { useGmvMaxMetricsSync } from './useGmvMaxMetricsSync.js';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false },
    },
  });
  return function Wrapper({ children }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe('useGmvMaxMetricsSync campaign scope', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    syncGmvMaxMetrics.mockResolvedValue({ task_id: 'task-1', state: 'SUCCESS' });
  });

  it('uses the campaign endpoint and discards payload campaign widening', async () => {
    const { result } = renderHook(
      () =>
        useGmvMaxMetricsSync({
          workspaceId: 'workspace-1',
          provider: 'tiktok-business',
          authId: 'auth-1',
          campaignId: 'campaign-current',
        }),
      { wrapper: createWrapper() },
    );

    await act(async () => {
      await result.current.startSyncAsync({
        start_date: '2026-07-21',
        end_date: '2026-07-21',
        levels: ['CAMPAIGN', 'PRODUCT', 'CREATIVE'],
        campaign_ids: ['campaign-other'],
        item_group_ids: ['item-1'],
      });
    });

    expect(syncGmvMaxMetrics).toHaveBeenCalledWith(
      'workspace-1',
      'tiktok-business',
      'auth-1',
      'campaign-current',
      {
        start_date: '2026-07-21',
        end_date: '2026-07-21',
        levels: ['CAMPAIGN', 'PRODUCT', 'CREATIVE'],
        campaign_ids: ['campaign-current'],
        item_group_ids: ['item-1'],
      },
    );
  });

  it('surfaces the backend account-lock waiting phase', async () => {
    syncGmvMaxMetrics.mockResolvedValue({
      task_id: 'task-waiting',
      state: 'RETRY',
      progress: {
        phase: 'WAITING_ACCOUNT_SYNC',
        message: '同账户定时同步正在收尾，当前系列将在锁释放后自动开始…',
      },
    });
    const { result } = renderHook(
      () =>
        useGmvMaxMetricsSync({
          workspaceId: 'workspace-1',
          provider: 'tiktok-business',
          authId: 'auth-1',
          campaignId: 'campaign-current',
        }),
      { wrapper: createWrapper() },
    );

    await act(async () => {
      await result.current.startSyncAsync({ levels: ['CAMPAIGN'] });
    });

    expect(result.current.isSyncing).toBe(true);
    expect(result.current.syncState).toBe('RETRY');
    expect(result.current.syncMessage).toBe(
      '同账户定时同步正在收尾，当前系列将在锁释放后自动开始…',
    );
  });
});
