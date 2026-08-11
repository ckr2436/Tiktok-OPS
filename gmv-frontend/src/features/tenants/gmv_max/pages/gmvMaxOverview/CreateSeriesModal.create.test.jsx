import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => {
  const createCampaign = vi.fn();
  const precheckCampaign = vi.fn();
  const optionsRefetch = vi.fn();
  return {
    createCampaign,
    precheckCampaign,
    optionsRefetch,
    createMutation: {
      isPending: false,
      mutateAsync: createCampaign,
    },
    identitiesQuery: {
      data: {
        identities: [
          {
            identity_id: 'identity-1',
            identity_name: '测试身份',
            identity_type: 'TT_USER',
          },
        ],
      },
      isLoading: false,
    },
    optionsQuery: {
      data: {
        stores: [
          {
            store_id: 'store-1',
            store_name: '测试店铺',
            currency: 'USD',
          },
        ],
      },
      error: null,
      isLoading: false,
      refetch: optionsRefetch,
    },
    precheckMutation: {
      isPending: false,
      mutateAsync: precheckCampaign,
    },
    productsQuery: {
      data: { items: [] },
      isFetching: false,
      isLoading: false,
    },
  };
});

vi.mock('../../hooks/gmvMaxQueries.js', () => ({
  useCreateGmvMaxCampaignMutation: () => mocks.createMutation,
  useGmvMaxIdentitiesQuery: () => mocks.identitiesQuery,
  useGmvMaxOptionsQuery: () => mocks.optionsQuery,
  useGmvMaxPrecheckMutation: () => mocks.precheckMutation,
  useProductsQuery: () => mocks.productsQuery,
}));

import CreateSeriesModal from './CreateSeriesModal.jsx';

const PRECHECK_RESULT = {
  is_gmv_max_available: true,
  needs_exclusive_auth: false,
  promote_all_products_allowed: true,
  recommended_budget: 200,
  recommended_roas_bid: 1.2,
};

function renderModal(overrides = {}) {
  return render(
    <CreateSeriesModal
      open
      onClose={vi.fn()}
      workspaceId="workspace-1"
      provider="tiktok-business"
      authId="auth-1"
      advertiserId="advertiser-1"
      storeId="store-1"
      storeNameById={new Map([['store-1', '测试店铺']])}
      onCreated={vi.fn()}
      {...overrides}
    />,
  );
}

function campaignCreateIntentKeys() {
  return Array.from({ length: window.localStorage.length }, (_, index) =>
    window.localStorage.key(index),
  ).filter((key) => key?.startsWith('gmvmax.campaign-create-intent.v1:'));
}

async function submitNewCreate() {
  const createButton = screen.getByRole('button', { name: '新建智能投放' });
  await waitFor(() => expect(createButton).toBeEnabled());
  fireEvent.click(createButton);
  await waitFor(() => expect(mocks.createCampaign).toHaveBeenCalledTimes(1));
  return mocks.createCampaign.mock.calls[0][0];
}

describe('CreateSeriesModal durable campaign creation', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    mocks.createCampaign.mockReset();
    mocks.precheckCampaign.mockReset();
    mocks.optionsRefetch.mockReset();
    mocks.precheckCampaign.mockResolvedValue(PRECHECK_RESULT);
  });

  it('sends one stable official request id and keeps the frozen payload after a timeout', async () => {
    mocks.createCampaign.mockRejectedValueOnce(new Error('network timeout'));
    renderModal();

    const payload = await submitNewCreate();

    expect(payload.request_id).toMatch(/^[1-9]\d{0,18}$/);
    expect(payload.idempotency_key).toBe(payload.request_id);
    expect(await screen.findByText('network timeout')).toBeInTheDocument();
    const [storageKey] = campaignCreateIntentKeys();
    expect(storageKey).toBeTruthy();
    expect(JSON.parse(window.localStorage.getItem(storageKey))).toEqual({
      campaign_name: payload.campaign_name,
      request_id: payload.request_id,
      create_payload: payload,
    });
  });

  it('retries the exact frozen payload after remount without rerunning precheck', async () => {
    mocks.createCampaign
      .mockRejectedValueOnce(new Error('network timeout'))
      .mockResolvedValueOnce({
        campaign: { campaign_id: 'campaign-1' },
        creation_status: 'SUCCEEDED',
      });
    const firstRender = renderModal();
    const firstPayload = await submitNewCreate();
    await screen.findByText('network timeout');
    const precheckCallsBeforeRemount = mocks.precheckCampaign.mock.calls.length;
    expect(precheckCallsBeforeRemount).toBeGreaterThan(0);

    firstRender.unmount();
    mocks.precheckCampaign.mockRejectedValue(
      new Error('a frozen retry must not require precheck'),
    );
    const onCreated = vi.fn();
    renderModal({ onCreated });

    const continueButton = await screen.findByRole('button', {
      name: '继续完成智能投放',
    });
    expect(continueButton).toBeEnabled();
    expect(screen.getByPlaceholderText('请输入预算')).toHaveValue(null);
    expect(screen.getByPlaceholderText('请输入 ROAS 出价')).toHaveValue(null);
    fireEvent.click(continueButton);

    await waitFor(() => expect(mocks.createCampaign).toHaveBeenCalledTimes(2));
    expect(mocks.createCampaign.mock.calls[1][0]).toEqual(firstPayload);
    expect(mocks.precheckCampaign).toHaveBeenCalledTimes(
      precheckCallsBeforeRemount,
    );
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(campaignCreateIntentKeys()).toHaveLength(0);
  });

  it('clears the durable intent after a successful create', async () => {
    mocks.createCampaign.mockResolvedValueOnce({
      campaign: { campaign_id: 'campaign-1' },
      creation_status: 'SUCCEEDED',
    });
    const onCreated = vi.fn();
    renderModal({ onCreated });

    await submitNewCreate();

    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(campaignCreateIntentKeys()).toHaveLength(0);
  });

  it.each(['QUARANTINED', 'QUARANTINE_PENDING'])(
    'keeps the same durable intent when post-create setup is %s',
    async (creationStatus) => {
      mocks.createCampaign.mockResolvedValueOnce({
        campaign: { campaign_id: 'campaign-quarantined' },
        creation_status: creationStatus,
        warnings: [{ message: '系列已创建并暂停，请重试完成智能策略。' }],
      });
      const onCreated = vi.fn();
      renderModal({ onCreated });

      const payload = await submitNewCreate();

      expect(
        await screen.findByText('系列已创建并暂停，请重试完成智能策略。'),
      ).toBeInTheDocument();
      expect(onCreated).not.toHaveBeenCalled();
      const [storageKey] = campaignCreateIntentKeys();
      expect(storageKey).toBeTruthy();
      expect(
        JSON.parse(window.localStorage.getItem(storageKey)).create_payload,
      ).toEqual(payload);
    },
  );
});
