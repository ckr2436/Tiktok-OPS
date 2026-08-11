import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const polling = vi.hoisted(() => ({
  terminalTask: null,
}));

vi.mock('../api/gmvMaxApi.js', () => ({
  startGmvMaxSync: vi.fn(),
  syncGmvMaxMetrics: vi.fn(),
}));

vi.mock('./useGmvTaskPolling.js', async () => {
  const React = await import('react');
  return {
    useGmvTaskPolling: ({ taskId, onSuccess, onFailure }) => {
      React.useEffect(() => {
        if (!taskId || !polling.terminalTask) return;
        const terminalTask = polling.terminalTask;
        polling.terminalTask = null;
        Promise.resolve().then(() => {
          if (terminalTask.state === 'SUCCESS') {
            onSuccess(terminalTask);
          } else {
            onFailure(terminalTask);
          }
        });
      }, [onFailure, onSuccess, taskId]);
      return {
        data: null,
        isFetching: Boolean(taskId),
      };
    },
  };
});

import { startGmvMaxSync, syncGmvMaxMetrics } from '../api/gmvMaxApi.js';
import { useEnsureFreshGmvData } from './useGmvSyncTask.js';

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

function renderSafetyHook(overrides = {}) {
  return renderHook(
    () =>
      useEnsureFreshGmvData({
        workspaceId: 'workspace-1',
        provider: 'tiktok-business',
        authId: 'auth-1',
        reportParams: {
          start_date: '2026-07-17',
          end_date: '2026-07-17',
        },
        levels: ['CAMPAIGN', 'PRODUCT', 'CREATIVE'],
        campaignIds: ['campaign-1'],
        itemGroupIds: ['item-group-1'],
        ...overrides,
      }),
    { wrapper: createWrapper() },
  );
}

describe('useEnsureFreshGmvData fail-closed preflight', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    polling.terminalTask = null;
  });

  it.each([
    ['the sync start response is missing', undefined],
    ['the sync start response has no task id', { state: 'PENDING' }],
    ['the sync start response is a terminal failure', { state: 'FAILURE', error: 'upstream failed' }],
  ])('returns false when %s', async (_label, response) => {
    startGmvMaxSync.mockResolvedValue(response);
    const { result } = renderSafetyHook();

    let fresh;
    await act(async () => {
      fresh = await result.current.ensureFresh();
    });

    expect(fresh).toBe(false);
    expect(startGmvMaxSync).toHaveBeenCalledWith(
      'workspace-1',
      'tiktok-business',
      'auth-1',
      {
        start_date: '2026-07-17',
        end_date: '2026-07-17',
        levels: ['CAMPAIGN', 'PRODUCT', 'CREATIVE'],
        campaign_ids: ['campaign-1'],
        item_group_ids: ['item-group-1'],
      },
    );
  });

  it('returns false rather than granting freshness when the sync request rejects', async () => {
    startGmvMaxSync.mockRejectedValue(new Error('network unavailable'));
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result } = renderSafetyHook();

    let fresh;
    await act(async () => {
      fresh = await result.current.ensureFresh();
    });

    expect(fresh).toBe(false);
    expect(consoleError).toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it('returns true only for a successful terminal response and caches that proof', async () => {
    startGmvMaxSync.mockResolvedValue({ task_id: 'task-1', state: 'SUCCESS' });
    const { result } = renderSafetyHook();

    let first;
    let second;
    await act(async () => {
      first = await result.current.ensureFresh();
      second = await result.current.ensureFresh();
    });

    expect(first).toBe(true);
    expect(second).toBe(true);
    expect(startGmvMaxSync).toHaveBeenCalledTimes(1);
  });

  it('uses the campaign endpoint for campaign-detail freshness checks', async () => {
    syncGmvMaxMetrics.mockResolvedValue({ task_id: 'task-campaign', state: 'SUCCESS' });
    const { result } = renderSafetyHook({ campaignId: 'campaign-1' });

    let fresh;
    await act(async () => {
      fresh = await result.current.ensureFresh();
    });

    expect(fresh).toBe(true);
    expect(syncGmvMaxMetrics).toHaveBeenCalledWith(
      'workspace-1',
      'tiktok-business',
      'auth-1',
      'campaign-1',
      {
        start_date: '2026-07-17',
        end_date: '2026-07-17',
        levels: ['CAMPAIGN', 'PRODUCT', 'CREATIVE'],
        campaign_ids: ['campaign-1'],
        item_group_ids: ['item-group-1'],
      },
    );
    expect(startGmvMaxSync).not.toHaveBeenCalled();
  });

  it('waits for an identified active task and returns true only after polling reports success', async () => {
    startGmvMaxSync.mockResolvedValue({ task_id: 'task-active', state: 'PENDING' });
    polling.terminalTask = { task_id: 'task-active', state: 'SUCCESS' };
    const { result } = renderSafetyHook();

    let fresh;
    await act(async () => {
      fresh = await result.current.ensureFresh();
    });

    expect(fresh).toBe(true);
  });

  it('returns false when polling reports that an identified active task failed', async () => {
    startGmvMaxSync.mockResolvedValue({ task_id: 'task-failed', state: 'PENDING' });
    polling.terminalTask = {
      task_id: 'task-failed',
      state: 'FAILURE',
      error: 'official API rejected sync',
    };
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result } = renderSafetyHook();

    let fresh;
    await act(async () => {
      fresh = await result.current.ensureFresh();
    });

    expect(fresh).toBe(false);
    expect(consoleError).toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it('returns false without starting a sync when account scope is incomplete', async () => {
    const { result } = renderSafetyHook({ authId: '' });

    let fresh;
    await act(async () => {
      fresh = await result.current.ensureFresh();
    });

    expect(fresh).toBe(false);
    expect(startGmvMaxSync).not.toHaveBeenCalled();
  });
});
