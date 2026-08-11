import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  useActionLogs: vi.fn(),
  refetch: vi.fn(),
}));

vi.mock('../hooks/gmvMaxQueries.js', () => ({
  useGmvMaxActionLogsQuery: (...args) => mocks.useActionLogs(...args),
}));

import ActionLogsTable from './ActionLogsTable.jsx';

describe('ActionLogsTable pagination', () => {
  beforeEach(() => {
    mocks.refetch.mockReset();
    mocks.useActionLogs.mockReset();
    mocks.useActionLogs.mockImplementation((_workspaceId, _provider, _authId, _campaignId, params) => ({
      data: {
        entries: [
          {
            id: `log-page-${params.page}`,
            timestamp: `2026-07-${String(params.page).padStart(2, '0')}T12:00:00Z`,
            action_type: 'PAUSE',
            campaign_id: 'campaign-1',
            reason: `第 ${params.page} 页记录`,
          },
        ],
        total: 45,
      },
      isLoading: false,
      isFetching: false,
      error: null,
      refetch: mocks.refetch,
    }));
  });

  it('requests the selected server page and exposes previous/next controls', async () => {
    render(
      <ActionLogsTable
        workspaceId="workspace-1"
        provider="tiktok-business"
        authId="auth-1"
        campaignId="campaign-1"
        pageSize={20}
      />,
    );

    expect(screen.getByText('第 1 页记录')).toBeInTheDocument();
    expect(screen.getByText('第 1 / 3 页')).toBeInTheDocument();
    expect(screen.getByText('共 45 条')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '下一页' }));

    await waitFor(() => {
      const latestParams = mocks.useActionLogs.mock.calls.at(-1)[4];
      expect(latestParams).toMatchObject({ page: 2, page_size: 20, sort: '-timestamp' });
    });
    expect(screen.getByText('第 2 页记录')).toBeInTheDocument();
    expect(screen.getByText('第 2 / 3 页')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '上一页' }));
    await waitFor(() => {
      expect(mocks.useActionLogs.mock.calls.at(-1)[4].page).toBe(1);
    });
  });
});
