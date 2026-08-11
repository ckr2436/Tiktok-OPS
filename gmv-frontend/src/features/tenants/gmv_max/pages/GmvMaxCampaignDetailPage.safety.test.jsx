import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  searchParams: new URLSearchParams(),
  ensureFresh: vi.fn(),
  applyAction: vi.fn(),
  updateStrategy: vi.fn(),
  startHeating: vi.fn(),
  stopHeating: vi.fn(),
  setSearchParams: vi.fn(),
  navigate: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  creativeAssets: [],
  creativeMetrics: [],
  creativeMetricsData: null,
  creativeHeating: [],
  products: [],
  advertisers: [],
  invalidateQueries: vi.fn(),
  refetchQueries: vi.fn(),
  campaignRefetch: vi.fn(),
  strategyRefetch: vi.fn(),
  strategyData: { enabled: false },
  strategyResponse: null,
  campaignData: null,
  metricsArgs: null,
  creativeAssetsParams: null,
  creativeAssetsOptions: null,
}));

const campaignData = {
  campaign: {
    campaign_id: 'campaign-1',
    campaign_name: '软糖投放',
    operation_status: 'ENABLE',
    budget: 200,
    roas_bid: 1.3,
    deep_bid_type: 'VO_MIN_ROAS',
    optimization_goal: 'VALUE',
    store_id: 'store-1',
    item_group_id: 'product-1',
  },
  sessions: [
    {
      session_id: 'session-1',
      product_list: [
        {
          spu_id: 'product-1',
          title: '软糖',
        },
      ],
    },
  ],
};

const emptyReportQuery = {
  data: { report: { list: [] } },
  isLoading: false,
  isFetching: false,
  error: null,
};

vi.mock('react-router-dom', () => ({
  useNavigate: () => mocks.navigate,
  useParams: () => ({ wid: 'workspace-1', campaignId: 'campaign-1' }),
  useSearchParams: () => [mocks.searchParams, mocks.setSearchParams],
}));

vi.mock('antd', () => ({
  message: {
    warning: mocks.warning,
    error: mocks.error,
    success: mocks.success,
  },
}));

vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({
    invalidateQueries: mocks.invalidateQueries,
    refetchQueries: mocks.refetchQueries,
  }),
}));

vi.mock('../hooks/useGmvSyncTask.js', () => ({
  useEnsureFreshGmvData: () => ({
    ensureFresh: mocks.ensureFresh,
    isSyncing: false,
    error: null,
  }),
}));

vi.mock('../hooks/useGmvMaxMetrics.js', () => ({
  useGmvMaxMetrics: (args) => {
    mocks.metricsArgs = args;
    return {
      campaignMetrics: emptyReportQuery,
      productMetrics: emptyReportQuery,
      creativeMetrics: {
        data: mocks.creativeMetricsData || { items: mocks.creativeMetrics },
        isLoading: false,
        isFetching: false,
        error: null,
      },
    };
  },
}));

vi.mock('../hooks/useGmvMaxMetricsSync.js', () => ({
  useGmvMaxMetricsSync: () => ({
    task: null,
    isSyncing: false,
    syncError: null,
    startSyncAsync: vi.fn(),
  }),
}));

vi.mock('../hooks/useGmvMaxNotifications.js', () => ({
  default: () => ({
    notification: null,
    dismiss: vi.fn(),
  }),
}));

vi.mock('../hooks/gmvMaxQueries.js', () => ({
  composeMetricsQueryBaseKey: (...parts) => ['gmvMax', 'metrics', ...parts],
  useAccountsQuery: () => ({
    data: { items: [{ id: 'auth-1', label: '授权账户' }] },
  }),
  useAdvertisersQuery: () => ({
    data: { items: mocks.advertisers },
  }),
  useStoresQuery: () => ({
    data: { items: [{ store_id: 'store-1', store_name: '软糖店铺' }] },
  }),
  useProductsQuery: () => ({
    data: { items: mocks.products },
    isLoading: false,
    isFetching: false,
    error: null,
  }),
  useGmvMaxCampaignQuery: () => ({
    data: mocks.campaignData || campaignData,
    isSuccess: true,
    refetch: mocks.campaignRefetch,
  }),
  useGmvMaxCreativeAssetsQuery: (_workspaceId, _provider, _authId, _campaignId, params, options) => {
    mocks.creativeAssetsParams = params;
    mocks.creativeAssetsOptions = options;
    return {
      data: { items: mocks.creativeAssets },
      isLoading: false,
      error: null,
    };
  },
  useGmvMaxCreativeHeatingQuery: () => ({
    data: { items: mocks.creativeHeating },
    isLoading: false,
    error: null,
  }),
  useGmvMaxStrategyQuery: () => ({
    data: mocks.strategyResponse,
    isLoading: false,
    isFetching: false,
    error: null,
    refetch: mocks.strategyRefetch,
  }),
  useApplyGmvMaxActionMutation: () => ({
    mutate: vi.fn(),
    mutateAsync: mocks.applyAction,
    isPending: false,
    error: null,
  }),
  useStartGmvMaxCreativeHeatingMutation: () => ({
    mutate: mocks.startHeating,
    isPending: false,
    error: null,
  }),
  useStopGmvMaxCreativeHeatingMutation: () => ({
    mutate: mocks.stopHeating,
    isPending: false,
    error: null,
  }),
  useUpdateGmvMaxStrategyMutation: () => ({
    mutate: vi.fn(),
    mutateAsync: mocks.updateStrategy,
    isPending: false,
    error: null,
  }),
  usePreviewGmvMaxStrategyMutation: () => ({
    mutate: vi.fn(),
    isPending: false,
    data: null,
    error: null,
  }),
}));

import GmvMaxCampaignDetailPage from './GmvMaxCampaignDetailPage.jsx';

function configurePage({
  tab = 'automation',
  ensureFresh = false,
  boosting = false,
  stopped = false,
  includeCreative = false,
  includeProductCard = false,
  includeMultipleProductCards = false,
  missingProductBinding = false,
  advertiserTimezone,
  selectedProductId,
} = {}) {
  mocks.searchParams = new URLSearchParams({
    provider: 'tiktok-business',
    authId: 'auth-1',
    advertiserId: 'advertiser-1',
    storeId: 'store-1',
    tab,
  });
  const resolvedSelectedProductId =
    selectedProductId ??
    (includeCreative || includeProductCard ? 'product-1' : '');
  if (resolvedSelectedProductId) {
    mocks.searchParams.set('productId', resolvedSelectedProductId);
  }
  mocks.ensureFresh.mockResolvedValue(ensureFresh);
  mocks.advertisers = advertiserTimezone
    ? [{ advertiser_id: 'advertiser-1', display_timezone: advertiserTimezone }]
    : [{ advertiser_id: 'advertiser-1' }];
  mocks.campaignData = missingProductBinding
    ? {
        campaign: {
          campaign_id: 'campaign-1',
          campaign_name: '软糖投放',
          operation_status: 'ENABLE',
          store_id: 'store-1',
        },
        sessions: [],
      }
    : includeMultipleProductCards
    ? {
        campaign: {
          ...campaignData.campaign,
          item_group_ids: ['product-1', 'product-2'],
        },
        sessions: [
          {
            session_id: 'session-1',
            product_list: [
              { spu_id: 'product-1', title: '软糖 A' },
              { spu_id: 'product-2', title: '软糖 B' },
            ],
          },
        ],
      }
    : campaignData;
  mocks.strategyResponse = {
    ...mocks.strategyData,
    campaign: mocks.campaignData?.campaign || campaignData.campaign,
    recommendation: { budget: 200, roas_bid: 2 },
  };
  mocks.creativeMetricsData = null;
  mocks.creativeAssets = includeCreative
    ? [
        {
          item_id: 'creative-1',
          item_group_id: 'product-1',
          title: '软糖素材',
          creative_delivery_status: 'DELIVERING',
          currency: 'USD',
        },
      ]
    : [];
  mocks.creativeMetrics = includeCreative
    ? [
        {
          creative_id: 'creative-1',
          product_id: 'product-1',
          creative_delivery_status: 'DELIVERING',
          metrics: {
            spend: 25,
            gmv: 50,
            orders: 2,
          },
        },
      ]
    : [];
  if (includeMultipleProductCards) {
    mocks.products = [
      {
        product_id: 'product-1',
        title: '软糖 A',
        image_url: 'https://cdn.example/product-a.jpg',
      },
      {
        product_id: 'product-2',
        title: '软糖 B',
        image_url: 'https://cdn.example/product-b.jpg',
      },
    ];
    mocks.creativeMetrics = [
      {
        creative_id: '-1',
        product_id: 'product-1',
        creative_delivery_status: 'DELIVERING',
        metrics: {
          spend: 3,
          gmv: 5,
          orders: 1,
          local_cover_url: '/api/v1/creative-assets/42/cover',
          local_preview_url: '/api/v1/creative-assets/42/video',
        },
      },
      {
        creative_id: '-1',
        product_id: 'product-2',
        creative_delivery_status: 'DELIVERING',
        metrics: {
          spend: 7,
          gmv: 11,
          orders: 2,
          local_cover_url: '/api/v1/creative-assets/84/cover',
          local_preview_url: '/api/v1/creative-assets/84/video',
        },
      },
    ];
  } else if (includeProductCard) {
    mocks.products = [
      {
        product_id: 'product-1',
        title: '软糖商品',
        image_url: 'https://cdn.example/product-thumbnail.jpg',
      },
    ];
    mocks.creativeMetrics = [
      {
        creative_id: '-1',
        product_id: 'product-1',
        creative_delivery_status: 'DELIVERING',
        metrics: {
          spend: 3,
          gmv: 5,
          orders: 1,
          local_cover_url: '/api/v1/creative-assets/42/cover',
          local_preview_url: '/api/v1/creative-assets/42/video',
        },
      },
    ];
  } else {
    mocks.products = [];
  }
  mocks.creativeHeating = boosting
    ? [
        {
          creative_id: 'creative-1',
          creative_status: 'DELIVERING',
          is_heating_active: true,
          status: 'HEATING',
        },
      ]
    : stopped
      ? [
          {
            creative_id: 'creative-1',
            creative_status: 'DELIVERING',
            is_heating_active: false,
            status: 'CANCELLED',
            mode: 'STOP',
            budget_delta: 0,
          },
        ]
      : [];
}

describe('GmvMaxCampaignDetailPage mutation preflight safety', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.metricsArgs = null;
    mocks.creativeAssetsParams = null;
    mocks.creativeAssetsOptions = null;
    configurePage();
    mocks.updateStrategy.mockResolvedValue({ status: 'success' });
  });

  it('edits the official daily budget and target ROI directly from campaign detail', async () => {
    render(<GmvMaxCampaignDetailPage />);

    fireEvent.click(screen.getByRole('button', { name: '编辑投放参数' }));
    const dialog = screen.getByRole('dialog', { name: '编辑投放参数' });
    const budgetInput = within(dialog).getByLabelText('每日预算');
    const roasInput = within(dialog).getByLabelText('目标 ROI');
    expect(budgetInput).toHaveValue(200);
    expect(roasInput).toHaveValue(1.3);

    fireEvent.change(budgetInput, { target: { value: '250' } });
    fireEvent.change(roasInput, { target: { value: '1.86' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '保存并同步官方' }));

    await waitFor(() => {
      expect(mocks.updateStrategy).toHaveBeenCalledWith({
        campaign: { budget: 250, roas_bid: 1.9 },
      });
    });
  });

  it('does not submit a budget change when freshness cannot be proven', async () => {
    configurePage({ ensureFresh: false });
    render(<GmvMaxCampaignDetailPage />);

    fireEvent.click(screen.getByRole('button', { name: '提升预算' }));
    fireEvent.click(screen.getByRole('button', { name: '确认调整' }));

    await waitFor(() => expect(mocks.ensureFresh).toHaveBeenCalledTimes(1));
    expect(mocks.applyAction).not.toHaveBeenCalled();
    expect(mocks.warning).toHaveBeenCalledWith(
      '实时数据尚未同步完成，预算未修改，请稍后重试。',
    );
  });

  it('does not start creative heating when freshness cannot be proven', async () => {
    configurePage({ tab: 'dashboard', ensureFresh: false, includeCreative: true });
    render(<GmvMaxCampaignDetailPage />);

    fireEvent.click(screen.getByRole('button', { name: '加热' }));
    const dialog = screen.getByRole('dialog', { name: '设置创意加热' });
    fireEvent.change(within(dialog).getByLabelText('每日加热预算'), {
      target: { value: '25' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: '确认并开始加热' }));

    await waitFor(() => expect(mocks.ensureFresh).toHaveBeenCalledTimes(1));
    expect(mocks.startHeating).not.toHaveBeenCalled();
    expect(mocks.warning).toHaveBeenCalledWith(
      '实时数据尚未同步完成，本次加热未执行，请稍后重试。',
    );
  });

  it('does not stop creative heating when freshness cannot be proven', async () => {
    configurePage({
      tab: 'dashboard',
      ensureFresh: false,
      includeCreative: true,
      boosting: true,
    });
    render(<GmvMaxCampaignDetailPage />);

    fireEvent.click(screen.getByRole('button', { name: '停止加热' }));

    await waitFor(() => expect(mocks.ensureFresh).toHaveBeenCalledTimes(1));
    expect(mocks.stopHeating).not.toHaveBeenCalled();
    expect(mocks.warning).toHaveBeenCalledWith(
      '实时数据尚未同步完成，本次停止操作未执行，请稍后重试。',
    );
  });

  it('builds a schema-compatible creative heating payload after successful preflight', async () => {
    configurePage({ tab: 'dashboard', ensureFresh: true, includeCreative: true });
    render(<GmvMaxCampaignDetailPage />);

    fireEvent.click(screen.getByRole('button', { name: '加热' }));
    const dialog = screen.getByRole('dialog', { name: '设置创意加热' });
    fireEvent.change(within(dialog).getByLabelText('每日加热预算'), {
      target: { value: '25' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: '确认并开始加热' }));

    await waitFor(() => {
      expect(mocks.startHeating).toHaveBeenCalledWith({
        creativeId: 'creative-1',
        payload: {
          mode: 'MANUAL',
          product_id: 'product-1',
          item_id: 'creative-1',
          currency: 'USD',
          target_daily_budget: 25,
          max_duration_minutes: 180,
        },
      });
    });
  });

  it('requires the operator to enter the independent daily heating budget', async () => {
    configurePage({ tab: 'dashboard', ensureFresh: true, includeCreative: true });
    render(<GmvMaxCampaignDetailPage />);

    fireEvent.click(screen.getByRole('button', { name: '加热' }));
    const dialog = screen.getByRole('dialog', { name: '设置创意加热' });
    expect(within(dialog).getByLabelText('每日加热预算')).toHaveValue(null);

    fireEvent.click(within(dialog).getByRole('button', { name: '确认并开始加热' }));

    expect(
      within(dialog).getByText('每日加热预算必须由用户填写，且不能低于 10。'),
    ).toBeInTheDocument();
    expect(mocks.ensureFresh).not.toHaveBeenCalled();
    expect(mocks.startHeating).not.toHaveBeenCalled();
  });

  it('restarts a previously stopped creative with a positive MANUAL payload', async () => {
    configurePage({
      tab: 'dashboard',
      ensureFresh: true,
      includeCreative: true,
      stopped: true,
    });
    render(<GmvMaxCampaignDetailPage />);

    fireEvent.click(screen.getByRole('button', { name: '加热' }));
    const dialog = screen.getByRole('dialog', { name: '设置创意加热' });
    fireEvent.change(within(dialog).getByLabelText('每日加热预算'), {
      target: { value: '30' },
    });
    fireEvent.change(within(dialog).getByLabelText('加热持续时间'), {
      target: { value: '360' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: '确认并开始加热' }));

    await waitFor(() => {
      expect(mocks.startHeating).toHaveBeenCalledWith({
        creativeId: 'creative-1',
        payload: {
          mode: 'MANUAL',
          product_id: 'product-1',
          item_id: 'creative-1',
          currency: 'USD',
          target_daily_budget: 30,
          max_duration_minutes: 360,
        },
      });
    });
  });

  it('keeps the official creative status separate from the local heating workflow status', () => {
    configurePage({ tab: 'dashboard', ensureFresh: true, includeCreative: true });
    mocks.creativeHeating = [
      {
        creative_id: 'creative-1',
        is_heating_active: true,
        status: 'APPLIED',
      },
    ];
    render(<GmvMaxCampaignDetailPage />);

    expect(screen.getAllByText('持续投放').length).toBeGreaterThan(0);
    expect(screen.getByTitle('加热中')).toBeInTheDocument();
    expect(screen.queryByText(/未知官方状态：APPLIED/)).not.toBeInTheDocument();
  });

  it('sends creative and product identity when stopping an active boost', async () => {
    configurePage({
      tab: 'dashboard',
      ensureFresh: true,
      includeCreative: true,
      boosting: true,
    });
    render(<GmvMaxCampaignDetailPage />);

    fireEvent.click(screen.getByRole('button', { name: '停止加热' }));

    await waitFor(() => {
      expect(mocks.stopHeating).toHaveBeenCalledWith({
        creativeId: 'creative-1',
        payload: {
          product_id: 'product-1',
          item_id: 'creative-1',
        },
      });
    });
  });

  it('labels a browser-timezone fallback honestly when advertiser timezone is absent', () => {
    configurePage({ tab: 'dashboard', advertiserTimezone: '' });
    render(<GmvMaxCampaignDetailPage />);

    expect(
      screen.getByText(/广告主时区未知，暂按浏览器时区：/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^按广告主时区：/)).not.toBeInTheDocument();
  });

  it('labels an advertiser timezone when metadata supplies one', () => {
    configurePage({ tab: 'dashboard', advertiserTimezone: 'America/Los_Angeles' });
    render(<GmvMaxCampaignDetailPage />);

    expect(
      screen.getByText('按广告主时区：America/Los_Angeles'),
    ).toBeInTheDocument();
  });

  it('uses the product thumbnail and never a video cover for creative id -1', () => {
    configurePage({ tab: 'dashboard', includeProductCard: true });
    render(<GmvMaxCampaignDetailPage />);

    const productCardId = screen.getByText('ID: 商品卡');
    const productCardRow = productCardId.closest('tr');
    expect(productCardRow).not.toBeNull();
    const thumbnail = within(productCardRow).getByRole('img', {
      name: '商品卡 · 软糖',
    });
    expect(thumbnail).toHaveAttribute(
      'src',
      'https://cdn.example/product-thumbnail.jpg',
    );
    expect(thumbnail.closest('a')).toBeNull();
  });

  it('lazily loads cached video covers at low priority', () => {
    configurePage({ tab: 'dashboard', includeCreative: true });
    mocks.creativeAssets[0].video_cover_url = '/api/v1/creative-assets/17/cover';
    render(<GmvMaxCampaignDetailPage />);

    const thumbnail = screen.getByRole('img', { name: '软糖素材' });
    expect(thumbnail).toHaveAttribute('src', '/api/v1/creative-assets/17/cover');
    expect(thumbnail).toHaveAttribute('loading', 'lazy');
    expect(thumbnail).toHaveAttribute('decoding', 'async');
    expect(thumbnail).toHaveAttribute('fetchpriority', 'low');
  });

  it('shows only the selected product card in a multi-product campaign', () => {
    configurePage({
      tab: 'dashboard',
      includeMultipleProductCards: true,
      selectedProductId: 'product-1',
    });
    render(<GmvMaxCampaignDetailPage />);

    const productA = screen.getByRole('img', {
      name: '商品卡 · 软糖 A',
    });
    expect(productA).toHaveAttribute('src', 'https://cdn.example/product-a.jpg');
    expect(productA.closest('a')).toBeNull();
    expect(within(productA.closest('tr')).getByText('$3.00')).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: '商品卡 · 软糖 B' })).not.toBeInTheDocument();
    expect(screen.getAllByText('ID: 商品卡')).toHaveLength(1);
    expect(mocks.metricsArgs.itemGroupIds).toEqual(['product-1']);
    expect(mocks.creativeAssetsParams.campaign_id).toBe('campaign-1');
    expect(mocks.creativeAssetsParams.item_group_ids).toEqual(['product-1']);
  });

  it('renders the product level first and opens a server-scoped product creative view', () => {
    configurePage({ tab: 'dashboard', includeMultipleProductCards: true });
    render(<GmvMaxCampaignDetailPage />);

    expect(screen.getByRole('table', { name: '系列商品列表' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看创意：软糖 A' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看创意：软糖 B' })).toBeInTheDocument();
    expect(screen.queryByText('当前筛选合计（0）')).not.toBeInTheDocument();
    expect(mocks.metricsArgs.creativeEnabled).toBe(false);
    expect(mocks.metricsArgs.itemGroupIds).toEqual([]);
    expect(mocks.creativeAssetsParams.item_group_ids).toBeUndefined();
    expect(mocks.creativeAssetsOptions.enabled).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: '查看创意：软糖 A' }));

    expect(screen.getByRole('button', { name: /返回商品列表/ })).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: '系列商品列表' })).not.toBeInTheDocument();
    expect(mocks.metricsArgs.itemGroupIds).toEqual(['product-1']);
    expect(mocks.creativeAssetsParams.item_group_ids).toEqual(['product-1']);
    expect(mocks.creativeAssetsOptions.enabled).toBe(true);
    expect(mocks.setSearchParams).toHaveBeenCalledWith(
      expect.objectContaining({}),
      { replace: true },
    );
    const nextParams = mocks.setSearchParams.mock.calls.at(-1)[0];
    expect(nextParams.get('productId')).toBe('product-1');

    fireEvent.click(screen.getByRole('button', { name: /返回商品列表/ }));
    expect(screen.getByRole('table', { name: '系列商品列表' })).toBeInTheDocument();
    const clearedParams = mocks.setSearchParams.mock.calls.at(-1)[0];
    expect(clearedParams.has('productId')).toBe(false);
  });

  it('labels campaign creatives separately from same-product candidates', () => {
    configurePage({ tab: 'dashboard', includeCreative: true });
    mocks.creativeAssets.push({
      item_id: 'candidate-1',
      title: '同商品历史素材',
      item_group_id: 'product-1',
    });
    render(<GmvMaxCampaignDetailPage />);

    expect(screen.getByText(/当前系列投放创意 1 · 同商品候选素材 1/)).toBeInTheDocument();
    expect(screen.getAllByText('同商品候选素材').length).toBeGreaterThan(0);
    expect(mocks.creativeAssetsParams.campaign_id).toBe('campaign-1');
  });

  it('uses official exploration semantics and preserves an unknown upstream status', () => {
    configurePage({ tab: 'dashboard', includeCreative: true });
    mocks.creativeAssets[0].creative_delivery_status = 'LEARNING';
    mocks.creativeMetrics[0].creative_delivery_status = 'LEARNING';
    const { unmount } = render(<GmvMaxCampaignDetailPage />);

    expect(screen.getAllByText('探索中').length).toBeGreaterThan(0);
    expect(screen.queryByText('学习中')).not.toBeInTheDocument();
    unmount();

    configurePage({ tab: 'dashboard', includeCreative: true });
    mocks.creativeAssets[0].creative_delivery_status = 'EXPLORING';
    mocks.creativeMetrics[0].creative_delivery_status = 'EXPLORING';
    render(<GmvMaxCampaignDetailPage />);

    expect(screen.getByText('未知官方状态：EXPLORING')).toBeInTheDocument();
    expect(screen.queryByText('同商品候选素材', { selector: '.gmvmax-status-pill' })).not.toBeInTheDocument();
  });

  it('shows no product rows instead of loading unscoped creative metrics when binding is absent', () => {
    configurePage({ tab: 'dashboard', missingProductBinding: true });
    render(<GmvMaxCampaignDetailPage />);

    expect(
      screen.getByText(/暂无系列商品数据，请先同步系列详情/),
    ).toBeInTheDocument();
    expect(mocks.metricsArgs.itemGroupIds).toEqual([]);
    expect(mocks.creativeAssetsParams.item_group_ids).toBeUndefined();
    expect(mocks.creativeAssetsOptions.enabled).toBe(false);
  });

  it('uses the product thumbnail for the production report.list response shape', () => {
    configurePage({ tab: 'dashboard', selectedProductId: 'product-1' });
    mocks.products = [
      {
        product_id: 'product-1',
        title: '软糖商品',
        image_url: 'https://cdn.example/report-product.jpg',
      },
    ];
    mocks.creativeMetricsData = {
      report: {
        list: [
          {
            dimensions: {
              creative_id: '-1',
              product_id: 'product-1',
            },
            metrics: {
              spend: 3,
              local_cover_url: '/api/v1/creative-assets/99/cover',
              local_preview_url: '/api/v1/creative-assets/99/video',
            },
          },
        ],
      },
    };
    render(<GmvMaxCampaignDetailPage />);

    const thumbnail = screen.getByRole('img', {
      name: '商品卡 · 软糖',
    });
    expect(thumbnail).toHaveAttribute(
      'src',
      'https://cdn.example/report-product.jpg',
    );
    expect(thumbnail.closest('a')).toBeNull();
  });
});
