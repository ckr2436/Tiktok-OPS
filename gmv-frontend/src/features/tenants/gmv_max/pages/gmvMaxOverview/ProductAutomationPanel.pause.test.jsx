import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  applyAction: vi.fn(),
  updateStrategy: vi.fn(),
}));

vi.mock('../../api/gmvMaxApi.js', () => ({
  applyGmvMaxAction: (...args) => mocks.applyAction(...args),
  createGmvMaxCampaign: vi.fn(),
  listGmvMaxCreativeAssets: vi.fn(),
  precheckGmvMaxCampaign: vi.fn(),
  refreshGmvMaxCreativeAssets: vi.fn(),
  updateGmvMaxStrategy: (...args) => mocks.updateStrategy(...args),
  uploadGmvMaxCreativeAsset: vi.fn(),
}));

vi.mock('../../../integrations/tiktok_business/service.js', () => ({
  listTikTokAccounts: vi.fn(() => Promise.resolve({ items: [] })),
}));

import ProductAutomationPanel from './ProductAutomationPanel.jsx';

function renderEnabledAutomation() {
  return render(
    <ProductAutomationPanel
      workspaceId="1"
      provider="tiktok-business"
      authId="2"
      advertiserId="adv-1"
      businessCenterId="bc-1"
      storeId="store-1"
      products={[{
        product_id: 'product-1',
        title: '测试商品',
        status: 'AVAILABLE',
        gmvmax_automation_stats: {
          latest_campaign_id: 'campaign-1',
          latest_campaign_name: '智能系列',
          campaign_operation_status: 'ENABLE',
          strategy_enabled: true,
        },
      }]}
      productsLoading={false}
      campaignCards={[]}
      canOperate
    />,
  );
}

describe('ProductAutomationPanel smart shutdown', () => {
  beforeEach(() => {
    window.localStorage.clear();
    mocks.applyAction.mockReset();
    mocks.updateStrategy.mockReset();
    mocks.applyAction.mockResolvedValue({ status: 'queued' });
  });

  it('submits one atomic shutdown intent when the official pause must queue', async () => {
    renderEnabledAutomation();
    const expandButton = screen.queryByRole('button', { name: '展开商品 测试商品' });
    if (expandButton) fireEvent.click(expandButton);

    fireEvent.click(screen.getByRole('button', { name: '关闭智能投放' }));

    await waitFor(() => expect(mocks.applyAction).toHaveBeenCalledWith(
      '1',
      'tiktok-business',
      '2',
      'campaign-1',
      { type: 'pause', disable_strategy: true },
    ));
    expect(mocks.applyAction).toHaveBeenCalledTimes(1);
    expect(mocks.updateStrategy).not.toHaveBeenCalled();
    expect(await screen.findByText(/智能策略已停用/)).toBeInTheDocument();
  });
});
