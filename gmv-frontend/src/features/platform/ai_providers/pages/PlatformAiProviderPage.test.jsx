import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../service.js', () => ({
  default: {
    listKeys: vi.fn(),
    listProviders: vi.fn(),
    getRoutingOverview: vi.fn(),
    listRoleGroups: vi.fn(),
    updateRoleProviderOrder: vi.fn(),
    listCatalogModels: vi.fn(),
    listRoutes: vi.fn(),
    listProviderModels: vi.fn(),
    createKey: vi.fn(),
    updateKey: vi.fn(),
    deactivateKey: vi.fn(),
    discoverKey: vi.fn(),
    discoverAll: vi.fn(),
    updateRoute: vi.fn(),
    probeRoute: vi.fn(),
    resetRouteCircuit: vi.fn(),
    updateProviderModel: vi.fn(),
  },
}))

import apiKeyApi from '../service.js'
import PlatformAiProviderPage from './PlatformAiProviderPage.jsx'


function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <PlatformAiProviderPage />
    </QueryClientProvider>,
  )
}


describe('PlatformAiProviderPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    apiKeyApi.listProviders.mockResolvedValue([
      { id: 'sub2api', label: 'Sub2API', capabilities: ['text', 'multimodal'] },
      { id: 'toapis', label: 'ToAPIs', capabilities: ['text', 'image', 'video'] },
      { id: 'coultra', label: 'Coultra', capabilities: ['text', 'multimodal'] },
    ])
    apiKeyApi.listKeys.mockResolvedValue([
      {
        id: 7,
        name: 'Hermes ToAPIs',
        provider_key: 'toapis',
        provider_label: 'ToAPIs',
        capabilities: ['text', 'image', 'video'],
        model_priorities: {},
        is_active: true,
        is_default: true,
        supports_model_discovery: true,
      },
    ])
    apiKeyApi.listProviderModels.mockResolvedValue([])
    apiKeyApi.listRoleGroups.mockResolvedValue([
      {
        role: 'ads_review',
        display_name: '广告复核',
        description: '重大广告决策复核',
        logical_model_id: 'gmv-ads-review-v1',
        workload: 'ads_review',
        capability: 'text',
        provider_order: ['sub2api', 'toapis', 'coultra'],
        eligible_route_count: 2,
        active_route: { provider_key: 'sub2api', provider_model_id: 'gpt-5.6-terra' },
        routes: [
          { source_logical_model_id: 'gpt-5.6-terra' },
          { source_logical_model_id: 'gpt-5.6-luna' },
        ],
      },
    ])
    apiKeyApi.listCatalogModels.mockResolvedValue({
      items: [{ id: 1, provider_key: 'toapis', provider_model_id: 'gpt-5.4-mini', capabilities: ['text', 'multimodal'], endpoint_modes: ['chat_completions'], lifecycle_status: 'VERIFIED', is_available: true }],
      page: 1,
      page_size: 50,
      total: 127,
    })
    apiKeyApi.listRoutes.mockImplementation(async ({ enabled }) => ({
      items: enabled === false
        ? [{ id: 10, key_id: 7, key_name: 'Hermes ToAPIs', key_active: true, provider_key: 'toapis', workload: 'default', logical_model_id: 'candidate-model', provider_model_id: 'candidate-model', capability: 'text', adapter_type: 'openai_chat_completions', priority: 100, is_enabled: false, is_verified: false, is_eligible: false, health_status: 'UNKNOWN' }]
        : [
          { id: 11, key_id: 8, key_name: 'Coultra gmv ops', key_active: true, provider_key: 'coultra', workload: 'video_analyst', logical_model_id: 'video-analyst-gpt-5.4-mini', provider_model_id: 'gpt-5.4-mini', capability: 'multimodal', adapter_type: 'openai_chat_completions', priority: 10, is_enabled: true, is_verified: true, is_eligible: true, health_status: 'HEALTHY' },
          { id: 9, key_id: 7, key_name: 'Hermes ToAPIs', key_active: true, provider_key: 'toapis', workload: 'video_analyst', logical_model_id: 'video-analyst-gpt-5.4-mini', provider_model_id: 'gpt-5.4-mini', capability: 'multimodal', adapter_type: 'openai_chat_completions', priority: 20, is_enabled: true, is_verified: true, is_eligible: true, health_status: 'HALF_OPEN', consecutive_failures: 1, total_successes: 3, total_failures: 1 },
          { id: 12, key_id: 7, key_name: 'Hermes ToAPIs', key_active: true, provider_key: 'toapis', workload: 'default', logical_model_id: 'writer-model', provider_model_id: 'writer-model', capability: 'text', adapter_type: 'openai_chat_completions', priority: 30, is_enabled: true, is_verified: true, is_eligible: true, health_status: 'HEALTHY' },
        ],
      page: 1,
      page_size: 50,
      total: enabled === false ? 1662 : 6,
    }))
    apiKeyApi.getRoutingOverview.mockResolvedValue({
      summary: { providers: 2, active_keys: 1, discovered_models: 127, available_models: 127, enabled_routes: 6, eligible_routes: 6, routes: 1668, open_circuits: 0 },
      providers: [
        { id: 'toapis', label: 'ToAPIs', capabilities: ['text', 'image', 'video'], active_key_count: 1, available_model_count: 127, healthy_route_count: 0 },
        { id: 'coultra', label: 'Coultra', capabilities: ['text', 'multimodal'], active_key_count: 0, available_model_count: 0, healthy_route_count: 0 },
      ],
      key_health: [{ key_id: 7, health_status: 'HALF_OPEN', route_count: 1, eligible_route_count: 1 }],
      recent_attempts: [{ id: 3, request_id: 'request-audit-3', provider_key: 'toapis', logical_model_id: 'video-analyst-gpt-5.4-mini', provider_model_id: 'gpt-5.4-mini', status: 'SUCCEEDED', latency_ms: 800, created_at: '2026-07-21T18:00:00' }],
    })
  })

  it('renders real route health and every professional workspace', async () => {
    renderPage()
    expect(await screen.findByText('AI 供应商与路由中心')).toBeInTheDocument()
    expect(await screen.findByText('127', { selector: '.ai-summary__item strong' })).toBeInTheDocument()
    expect(await screen.findByText('广告复核')).toBeInTheDocument()
    expect(screen.getByText('Sub2API / gpt-5.6-terra')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '供应商与 Key' }))
    expect(await screen.findByText('恢复试探')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '模型目录' }))
    expect(await screen.findByText('gpt-5.4-mini')).toBeInTheDocument()
    expect(screen.getByText('已验证')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '路由策略' }))
    expect((await screen.findAllByText('video-analyst-gpt-5.4-mini')).length).toBeGreaterThan(1)
    expect(screen.getByText('主路由')).toBeInTheDocument()
    expect(screen.getByText('备用 1')).toBeInTheDocument()
    expect(screen.getByText('单一路由')).toBeInTheDocument()
    expect(screen.getByDisplayValue('10')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '健康与审计' }))
    expect(await screen.findByText('request-audit-3')).toBeInTheDocument()
    expect(screen.getByText('800 ms')).toBeInTheDocument()
  })

  it('lets an administrator reorder providers inside a business role', async () => {
    apiKeyApi.updateRoleProviderOrder.mockResolvedValue({})
    renderPage()
    await screen.findByText('广告复核')
    fireEvent.click(screen.getByRole('button', { name: '广告复核 toapis 上移' }))
    await waitFor(() => expect(apiKeyApi.updateRoleProviderOrder).toHaveBeenCalledWith(
      'ads_review',
      ['toapis', 'sub2api', 'coultra'],
    ))
  })

  it('loads paged model and route datasets from dedicated endpoints', async () => {
    renderPage()
    await screen.findByText('AI 供应商与路由中心')
    expect(apiKeyApi.getRoutingOverview).toHaveBeenCalledWith({ include_details: false })
    expect(apiKeyApi.listCatalogModels).toHaveBeenCalledWith({ page: 1, page_size: 50, search: undefined, capability: undefined })
    expect(apiKeyApi.listRoutes).toHaveBeenCalledWith({ page: 1, page_size: 50, search: undefined, enabled: true })

    fireEvent.click(screen.getByRole('button', { name: '模型目录' }))
    expect(await screen.findByText('共 127 条 · 第 1/3 页')).toBeInTheDocument()
  })

  it('shows active routes first and separates the candidate pool', async () => {
    renderPage()
    await screen.findByText('AI 供应商与路由中心')
    fireEvent.click(screen.getByRole('button', { name: '路由策略' }))
    expect(await screen.findByText('当前实际调用配置')).toBeInTheDocument()
    expect(screen.getByText(/只有同组路由才比较优先级/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /正在使用\s*6/ })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByRole('button', { name: /候选路由\s*1662/ }))
    expect(await screen.findByText('candidate-model')).toBeInTheDocument()
    expect(screen.getByText('候选模型池')).toBeInTheDocument()
    expect(apiKeyApi.listRoutes).toHaveBeenCalledWith({ page: 1, page_size: 50, search: undefined, enabled: false })
  })

  it('keeps key validation errors inside the credential dialog', async () => {
    renderPage()
    await screen.findByText('AI 供应商与路由中心')
    fireEvent.click(screen.getByRole('button', { name: '新增 API Key' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(within(dialog).getByText('请填写名称和 API Key')).toBeInTheDocument())
    expect(apiKeyApi.createKey).not.toHaveBeenCalled()
  })
})
