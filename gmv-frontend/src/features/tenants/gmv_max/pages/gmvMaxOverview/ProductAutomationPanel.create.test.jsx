import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  applyAction: vi.fn(),
  createCampaign: vi.fn(),
  precheckCampaign: vi.fn(),
  updateStrategy: vi.fn(),
}));

vi.mock('../../api/gmvMaxApi.js', () => ({
  applyGmvMaxAction: (...args) => mocks.applyAction(...args),
  createGmvMaxCampaign: (...args) => mocks.createCampaign(...args),
  listGmvMaxCreativeAssets: vi.fn(),
  precheckGmvMaxCampaign: (...args) => mocks.precheckCampaign(...args),
  refreshGmvMaxCreativeAssets: vi.fn(),
  updateGmvMaxStrategy: (...args) => mocks.updateStrategy(...args),
  uploadGmvMaxCreativeAsset: vi.fn(),
}));

vi.mock('../../../integrations/tiktok_business/service.js', () => ({
  listTikTokAccounts: vi.fn(() => Promise.resolve({ items: [] })),
}));

import ProductAutomationPanel from './ProductAutomationPanel.jsx';

const DEFAULT_PRODUCT = {
  product_id: 'product-1',
  title: '测试商品',
  effective_price: 13,
  status: 'AVAILABLE',
  gmv_max_ads_status: 'UNOCCUPIED',
};

function renderPanel(overrides = {}) {
  return render(
    <ProductAutomationPanel
      workspaceId="1"
      provider="tiktok-business"
      authId="2"
      advertiserId="advertiser-1"
      businessCenterId="bc-1"
      storeId="store-1"
      products={[DEFAULT_PRODUCT]}
      productsLoading={false}
      campaignCards={[]}
      canOperate
      {...overrides}
    />,
  );
}

function startSmartCreate() {
  const expandButton = screen.queryByRole('button', { name: '展开商品 测试商品' });
  if (expandButton) fireEvent.click(expandButton);
  fireEvent.click(screen.getByRole('button', { name: '新建投放' }));
  fireEvent.click(screen.getByRole('button', { name: /新建智能投放.*Hermes/ }));
  fireEvent.click(screen.getByRole('button', { name: '新建智能投放' }));
}

function campaignCreateIntentKeys() {
  return Array.from({ length: window.localStorage.length }, (_, index) =>
    window.localStorage.key(index),
  ).filter((key) => key?.startsWith('gmvmax.campaign-create-intent.v1:'));
}

describe('ProductAutomationPanel campaign creation result', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    mocks.applyAction.mockReset();
    mocks.createCampaign.mockReset();
    mocks.precheckCampaign.mockReset();
    mocks.updateStrategy.mockReset();
    mocks.precheckCampaign.mockResolvedValue({
      is_gmv_max_available: true,
      needs_exclusive_auth: false,
      recommended_roas_bid: 1.2,
      recommended_budget: 200,
      available_identities: [
        {
          identity_id: 'identity-1',
          identity_type: 'TT_USER',
        },
      ],
    });
    mocks.createCampaign.mockResolvedValue({
      campaign: { campaign_id: 'campaign-new' },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('keeps the create success result when the follow-up page refresh fails', async () => {
    const onChanged = vi.fn().mockRejectedValue(new Error('refresh failed'));
    renderPanel({ onChanged });
    startSmartCreate();

    await waitFor(() => expect(mocks.createCampaign).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText(/智能投放已创建.*页面数据刷新稍有延迟/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/智能投放开启失败/)).not.toBeInTheDocument();
    expect(campaignCreateIntentKeys()).toHaveLength(0);
  });

  it('creates an image-only Product GMV Max campaign when no identity is available', async () => {
    mocks.precheckCampaign.mockResolvedValue({
      is_gmv_max_available: true,
      needs_exclusive_auth: false,
      recommended_roas_bid: 1.2,
      recommended_budget: 200,
      available_identities: [],
    });
    renderPanel();
    startSmartCreate();

    await waitFor(() => expect(mocks.createCampaign).toHaveBeenCalledTimes(1));
    const payload = mocks.createCampaign.mock.calls[0][3];
    expect(payload.identity_list).toBeUndefined();
    expect(payload.request_id).toMatch(/^[1-9]\d{0,18}$/);
    expect(payload.idempotency_key).toBe(payload.request_id);
    expect(await screen.findByText(/智能投放已创建/)).toBeInTheDocument();
  });

  it('keeps a paused campaign when creating a replacement campaign', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPanel({
      products: [{ ...DEFAULT_PRODUCT, gmv_max_ads_status: 'OCCUPIED' }],
      campaignCards: [
        {
          campaign: {
            campaign_id: 'campaign-paused',
            campaign_name: '已暂停系列',
            operation_status: 'DISABLE',
            item_group_ids: ['product-1'],
          },
        },
      ],
    });
    startSmartCreate();

    await waitFor(() => expect(mocks.createCampaign).toHaveBeenCalledTimes(1));
    expect(mocks.applyAction).not.toHaveBeenCalled();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it.each(['QUARANTINED', 'QUARANTINE_PENDING'])(
    'keeps the same intent when a created campaign is %s',
    async (creationStatus) => {
      mocks.createCampaign.mockResolvedValue({
        campaign: { campaign_id: 'campaign-quarantined' },
        creation_status: creationStatus,
        warnings: [{ message: '系列已创建并暂停，请重试完成智能策略。' }],
      });
      const onChanged = vi.fn();
      renderPanel({ onChanged });
      startSmartCreate();

      expect(
        await screen.findByText('系列已创建并暂停，请重试完成智能策略。'),
      ).toBeInTheDocument();
      expect(campaignCreateIntentKeys()).toHaveLength(1);
      expect(onChanged).toHaveBeenCalledTimes(1);
    },
  );

  it('resumes a quarantined create intent instead of using ordinary restore actions', async () => {
    mocks.createCampaign
      .mockResolvedValueOnce({
        campaign: { campaign_id: 'campaign-quarantined' },
        creation_status: 'QUARANTINED',
        warnings: [{ message: '系列已创建并暂停，请重试完成智能策略。' }],
      })
      .mockResolvedValueOnce({
        campaign: { campaign_id: 'campaign-quarantined' },
        creation_status: 'SUCCEEDED',
      });

    const firstRender = renderPanel();
    startSmartCreate();
    await screen.findByText('系列已创建并暂停，请重试完成智能策略。');
    const firstPayload = mocks.createCampaign.mock.calls[0][3];
    firstRender.unmount();

    renderPanel({
      products: [{ ...DEFAULT_PRODUCT, gmv_max_ads_status: 'OCCUPIED' }],
      campaignCards: [
        {
          campaign: {
            campaign_id: 'campaign-quarantined',
            campaign_name: '隔离系列',
            operation_status: 'DISABLE',
            item_group_ids: ['product-1'],
          },
        },
      ],
    });
    const expandButton = screen.queryByRole('button', { name: '展开商品 测试商品' });
    if (expandButton) fireEvent.click(expandButton);
    fireEvent.click(screen.getByRole('button', { name: '恢复投放' }));
    fireEvent.click(screen.getByRole('button', { name: /智能模式恢复.*Hermes/ }));
    fireEvent.click(screen.getByRole('button', { name: '智能模式恢复' }));

    await waitFor(() => expect(mocks.createCampaign).toHaveBeenCalledTimes(2));
    expect(mocks.createCampaign.mock.calls[1][3]).toEqual(firstPayload);
    expect(mocks.updateStrategy).not.toHaveBeenCalled();
    expect(mocks.applyAction).not.toHaveBeenCalled();
    expect(campaignCreateIntentKeys()).toHaveLength(0);
  });

  it('clears the intent after a definitive create rejection', async () => {
    const rejection = new Error('invalid campaign parameters');
    rejection.response = {
      status: 400,
      data: {
        detail: {
          code: 'tiktok_error',
          details: { code: 'INVALID_ARGUMENT' },
        },
      },
    };
    mocks.createCampaign.mockRejectedValue(rejection);
    renderPanel();
    startSmartCreate();

    await waitFor(() => expect(mocks.createCampaign).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(campaignCreateIntentKeys()).toHaveLength(0));
  });

  it('reuses the create request id and campaign name after a failed request and remount', async () => {
    mocks.createCampaign
      .mockRejectedValueOnce(new Error('network timeout'))
      .mockResolvedValueOnce({ campaign: { campaign_id: 'campaign-new' } });

    const firstRender = renderPanel();
    startSmartCreate();

    expect(await screen.findByText('network timeout')).toBeInTheDocument();
    await waitFor(() => expect(mocks.createCampaign).toHaveBeenCalledTimes(1));
    const firstPayload = mocks.createCampaign.mock.calls[0][3];
    expect(campaignCreateIntentKeys()).toHaveLength(1);
    const [storedIntentKey] = campaignCreateIntentKeys();
    const storedIntent = JSON.parse(
      window.localStorage.getItem(storedIntentKey),
    );
    expect(storedIntent.create_payload).toEqual(firstPayload);
    expect(firstPayload.idempotency_key).toBe(firstPayload.request_id);

    firstRender.unmount();
    window.sessionStorage.clear();
    mocks.precheckCampaign.mockResolvedValue({
      is_gmv_max_available: true,
      needs_exclusive_auth: false,
      recommended_roas_bid: 9.9,
      recommended_budget: 999,
      available_identities: [
        {
          identity_id: 'identity-changed',
          identity_type: 'TT_USER',
        },
      ],
    });
    renderPanel();
    startSmartCreate();

    await waitFor(() => expect(mocks.createCampaign).toHaveBeenCalledTimes(2));
    const retryPayload = mocks.createCampaign.mock.calls[1][3];
    expect(retryPayload).toEqual(firstPayload);
    expect(mocks.precheckCampaign).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/智能投放已创建/)).toBeInTheDocument();
    expect(campaignCreateIntentKeys()).toHaveLength(0);
  });
});
