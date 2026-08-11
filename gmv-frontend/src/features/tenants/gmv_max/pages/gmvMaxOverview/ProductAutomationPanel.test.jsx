import { render, screen, within } from '@testing-library/react';

import ProductAutomationPanel from './ProductAutomationPanel.jsx';


function renderPanel({
  statsStatus,
  campaignStatus,
  includeCampaign = true,
  campaignCards: cardsOverride,
  omitAutomationStats = false,
  productOverrides = {},
}) {
  const product = {
    product_id: 'product-1',
    title: '测试商品',
    ...(omitAutomationStats
      ? {}
      : {
          gmvmax_automation_stats: {
            latest_campaign_id: 'campaign-1',
            campaign_operation_status: statsStatus,
            strategy_enabled: false,
          },
        }),
    ...productOverrides,
  };
  const campaignCards = cardsOverride || (includeCampaign
    ? [
      {
        campaign: {
          campaign_id: 'campaign-1',
          campaign_name: '测试系列',
          operation_status: campaignStatus,
        },
      },
    ]
    : []);

  return render(
    <ProductAutomationPanel
      workspaceId="1"
      provider="tiktok-business"
      authId="2"
      advertiserId="advertiser-1"
      businessCenterId="bc-1"
      storeId="store-1"
      products={[product]}
      productsLoading={false}
      campaignCards={campaignCards}
      canOperate={false}
    />,
  );
}


describe('ProductAutomationPanel campaign status', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('uses the canonical automation status for the matching latest campaign', () => {
    renderPanel({ statsStatus: 'DISABLE', campaignStatus: 'ENABLE' });

    expect(screen.getByText('普通投放已暂停')).toBeInTheDocument();
    expect(screen.queryByText('普通投放中')).not.toBeInTheDocument();
  });

  it('uses paused automation stats when the running-series filter omits the campaign card', () => {
    renderPanel({ statsStatus: 'DISABLE', includeCampaign: false });

    expect(screen.getByText('普通投放已暂停')).toBeInTheDocument();
    expect(screen.queryByText('普通投放中')).not.toBeInTheDocument();
  });

  it('does not fall back to an older enabled campaign when the latest paused card is filtered out', () => {
    renderPanel({
      statsStatus: 'DISABLE',
      campaignCards: [
        {
          campaign: {
            campaign_id: 'campaign-old',
            campaign_name: '旧运行系列',
            operation_status: 'ENABLE',
            item_group_ids: ['product-1'],
          },
        },
      ],
    });

    expect(screen.getByText('普通投放已暂停')).toBeInTheDocument();
    expect(screen.queryByText('普通投放中')).not.toBeInTheDocument();
    expect(screen.queryByText('旧运行系列')).not.toBeInTheDocument();
  });

  it('does not display campaign-wide performance as product metrics when canonical product stats are missing', () => {
    renderPanel({
      campaignStatus: 'ENABLE',
      omitAutomationStats: true,
      campaignCards: [
        {
          campaign: {
            campaign_id: 'campaign-1',
            campaign_name: '测试系列',
            operation_status: 'ENABLE',
            item_group_ids: ['product-1'],
          },
          performance: {
            spend: 123.45,
            gmv: 987.65,
          },
        },
      ],
    });

    const summary = screen.getByRole('button', { name: /商品 测试商品/ });
    const metricValues = within(summary).getAllByText('—');

    expect(metricValues).toHaveLength(2);
    expect(within(summary).queryByText('$123.45')).not.toBeInTheDocument();
    expect(within(summary).queryByText('$987.65')).not.toBeInTheDocument();
  });

  it('treats an officially occupied product as ordinary advertising and forces its card open', () => {
    window.localStorage.setItem(
      'gmvmax.product-cards.expanded.v1:1:store-1',
      JSON.stringify([]),
    );
    renderPanel({
      includeCampaign: false,
      omitAutomationStats: true,
      productOverrides: {
        gmv_max_ads_status: 'OCCUPIED',
        effective_price: 14.99,
        effective_price_source: 'tiktok_shop_latest_transaction',
      },
    });

    expect(screen.getByText('普通投放中')).toBeInTheDocument();
    expect(screen.queryByText('已占用')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '收起商品 测试商品' })).toBeInTheDocument();
    expect(screen.getAllByText(/14\.99/).length).toBeGreaterThan(0);
    expect(screen.getByText('TikTok Shop 最新成交价')).toBeInTheDocument();
    expect(screen.queryByRole('spinbutton', { name: /参考成交价/ })).not.toBeInTheDocument();
  });

  it('uses all-campaign active counts when the active campaign is absent from the card list', () => {
    renderPanel({
      includeCampaign: false,
      productOverrides: {
        gmvmax_automation_stats: {
          latest_campaign_id: 'campaign-active',
          latest_campaign_name: '普通系列',
          campaign_operation_status: 'ENABLE',
          active_campaign_count: 1,
          lifetime_campaign_count: 3,
          strategy_enabled: false,
          metric_scope: 'ALL_CAMPAIGNS_PRODUCT',
        },
      },
    });

    expect(screen.getByText('普通投放中')).toBeInTheDocument();
    expect(screen.getByText('普通系列')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '收起商品 测试商品' })).toBeInTheDocument();
  });
});
