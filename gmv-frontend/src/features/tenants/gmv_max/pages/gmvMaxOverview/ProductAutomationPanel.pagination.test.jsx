import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  listCreativeAssets: vi.fn(),
  refreshCreativeAssets: vi.fn(),
}));

vi.mock('../../api/gmvMaxApi.js', () => ({
  applyGmvMaxAction: vi.fn(),
  createGmvMaxCampaign: vi.fn(),
  listGmvMaxCreativeAssets: (...args) => mocks.listCreativeAssets(...args),
  precheckGmvMaxCampaign: vi.fn(),
  refreshGmvMaxCreativeAssets: (...args) => mocks.refreshCreativeAssets(...args),
  updateGmvMaxStrategy: vi.fn(),
  uploadGmvMaxCreativeAsset: vi.fn(),
}));

vi.mock('../../../integrations/tiktok_business/service.js', () => ({
  listTikTokAccounts: vi.fn(() => Promise.resolve({ items: [] })),
}));

import ProductAutomationPanel from './ProductAutomationPanel.jsx';

describe('ProductAutomationPanel creative pagination', () => {
  beforeEach(() => {
    window.localStorage.clear();
    mocks.listCreativeAssets.mockReset();
    mocks.refreshCreativeAssets.mockReset();
    mocks.listCreativeAssets.mockResolvedValue({
      items: [{ item_id: 'creative-1', selectable: true }],
      uploads: [],
      hermes: { evaluated: 1 },
    });
    mocks.refreshCreativeAssets.mockResolvedValue({
      sync: { upserted: 148 },
    });
  });

  it('loads every local page and refreshes through the admin POST endpoint', async () => {
    render(
      <ProductAutomationPanel
        workspaceId="1"
        provider="tiktok-business"
        authId="2"
        advertiserId="advertiser-1"
        businessCenterId="bc-1"
        storeId="store-1"
        products={[{ product_id: 'product-1', title: '测试商品' }]}
        productsLoading={false}
        campaignCards={[]}
        canOperate
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '展开商品 测试商品' }));
    fireEvent.click(screen.getByRole('button', { name: '指定视频' }));

    await waitFor(() => expect(mocks.listCreativeAssets).toHaveBeenCalledTimes(1));
    expect(mocks.listCreativeAssets.mock.calls[0][3]).toMatchObject({
      store_id: 'store-1',
      advertiser_id: 'advertiser-1',
      item_group_id: 'product-1',
      page_size: 100,
      fetch_all_pages: true,
    });
    expect(mocks.listCreativeAssets.mock.calls[0][3]).not.toHaveProperty('refresh');

    fireEvent.click(screen.getByRole('button', { name: '刷新素材' }));

    await waitFor(() => expect(mocks.refreshCreativeAssets).toHaveBeenCalledTimes(1));
    expect(mocks.refreshCreativeAssets.mock.calls[0][3]).toEqual({
      store_id: 'store-1',
      advertiser_id: 'advertiser-1',
      item_group_id: 'product-1',
    });
    await waitFor(() => expect(mocks.listCreativeAssets).toHaveBeenCalledTimes(2));
  });

  it('renders, searches, and selects candidates beyond the former six-item cap', async () => {
    mocks.listCreativeAssets.mockResolvedValue({
      items: Array.from({ length: 8 }, (_, index) => ({
        item_id: `creative-${index + 1}`,
        title: `素材 ${index + 1}`,
        selectable: true,
      })),
      uploads: [],
      hermes: { evaluated: 8 },
    });

    render(
      <ProductAutomationPanel
        workspaceId="1"
        provider="tiktok-business"
        authId="2"
        advertiserId="advertiser-1"
        businessCenterId="bc-1"
        storeId="store-1"
        products={[{ product_id: 'product-1', title: '测试商品' }]}
        productsLoading={false}
        campaignCards={[]}
        canOperate
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '展开商品 测试商品' }));
    fireEvent.click(screen.getByRole('button', { name: '指定视频' }));

    expect(await screen.findByText('素材 8')).toBeInTheDocument();
    expect(screen.getByText('显示 8 / 共 8 个')).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('搜索视频标题或 ID'), {
      target: { value: 'creative-8' },
    });

    expect(screen.queryByText('素材 1')).not.toBeInTheDocument();
    const eighthCandidate = screen.getByText('素材 8').closest('button');
    expect(eighthCandidate).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(eighthCandidate);
    expect(eighthCandidate).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('已选 1 个视频')).toBeInTheDocument();
  });
});
