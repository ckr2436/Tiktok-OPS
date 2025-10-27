import { describe, expect, beforeEach, vi, it } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import PlatformPolicies from '../pages/PlatformPolicies.jsx'
import {
  listPolicyProviders,
  listPolicies,
  createPolicy,
  updatePolicy,
  togglePolicy,
  deletePolicy,
  dryRunPolicy,
} from '../service.js'

vi.mock('../service.js', () => ({
  listPolicyProviders: vi.fn(),
  listPolicies: vi.fn(),
  createPolicy: vi.fn(),
  updatePolicy: vi.fn(),
  togglePolicy: vi.fn(),
  deletePolicy: vi.fn(),
  dryRunPolicy: vi.fn(),
}))

const providerFixtures = [
  { key: 'tiktok-business', name: 'TikTok Business', is_enabled: true },
]

const policyFixtures = [
  {
    id: 1,
    provider_key: 'tiktok-business',
    mode: 'WHITELIST',
    enforcement_mode: 'ENFORCE',
    domains: ['api.example.com'],
    business_scopes: { include: { bc_ids: ['123'] }, exclude: {} },
    limits: { rate_limit_rps: 10, cooldown_seconds: 5 },
    is_enabled: true,
    updated_at: '2025-01-01T00:00:00.000Z',
  },
]

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={<PlatformPolicies />} />
      </Routes>
    </MemoryRouter>
  )
}

describe('PlatformPolicies page', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    listPolicyProviders.mockResolvedValue(providerFixtures)
    listPolicies.mockResolvedValue({
      items: policyFixtures,
      total: policyFixtures.length,
      page: 1,
      page_size: 20,
    })
  })

  it('renders filters and table with normalized data', async () => {
    renderPage()
    await waitFor(() => expect(listPolicies).toHaveBeenCalledTimes(1))

    expect(screen.getByLabelText('提供方')).toBeInTheDocument()
    expect(screen.getByLabelText('模式')).toBeInTheDocument()
    expect(screen.getByLabelText('状态')).toBeInTheDocument()

    const row = await screen.findByRole('row', { name: /tiktok-business/i })
    expect(within(row).getByRole('cell', { name: 'tiktok-business' })).toBeInTheDocument()
    expect(within(row).getByRole('cell', { name: 'WHITELIST' })).toBeInTheDocument()
    expect(within(row).getByRole('cell', { name: '1' })).toBeInTheDocument()
  })

  it('creates a new policy via modal', async () => {
    listPolicies
      .mockResolvedValueOnce({ items: [], total: 0, page: 1, page_size: 20 })
      .mockResolvedValue({ items: policyFixtures, total: 1, page: 1, page_size: 20 })
    createPolicy.mockResolvedValue({})

    renderPage()
    await waitFor(() => expect(listPolicies).toHaveBeenCalledTimes(1))

    await userEvent.click(screen.getByText('新建策略'))
    const modal = await screen.findByRole('dialog')
    await userEvent.selectOptions(within(modal).getByLabelText('提供方'), 'tiktok-business')
    await userEvent.clear(within(modal).getByLabelText('策略名称'))
    await userEvent.type(within(modal).getByLabelText('策略名称'), 'UI Test Policy')
    const domainInput = within(modal).getByPlaceholderText('例如：api.example.com 或 *.example.com')
    await userEvent.type(domainInput, 'API.TEST.COM')
    await userEvent.tab()
    await userEvent.clear(within(modal).getByLabelText('速率限制 (RPS)'))
    await userEvent.type(within(modal).getByLabelText('速率限制 (RPS)'), '20')

    await userEvent.click(within(modal).getByRole('button', { name: '保存' }))

    await waitFor(() => {
      expect(createPolicy).toHaveBeenCalledWith({
        provider_key: 'tiktok-business',
        name: 'UI Test Policy',
        mode: 'WHITELIST',
        enforcement_mode: 'ENFORCE',
        domains: ['api.test.com'],
        business_scopes: {},
        description: '',
        is_enabled: true,
        rate_limit_rps: 20,
        rate_burst: null,
        cooldown_seconds: 0,
        max_concurrency: null,
        max_entities_per_run: null,
        window_cron: null,
      })
    })

    await waitFor(() => expect(listPolicies).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('策略已创建')).toBeInTheDocument()
  })

  it('shows validation error when domains are missing', async () => {
    createPolicy.mockResolvedValue({})
    renderPage()
    await waitFor(() => expect(listPolicies).toHaveBeenCalledTimes(1))

    await userEvent.click(screen.getByText('新建策略'))
    const modal = await screen.findByRole('dialog')
    await userEvent.selectOptions(within(modal).getByLabelText('提供方'), 'tiktok-business')
    await userEvent.clear(within(modal).getByLabelText('策略名称'))
    await userEvent.type(within(modal).getByLabelText('策略名称'), 'Missing Domain Policy')

    await userEvent.click(within(modal).getByRole('button', { name: '保存' }))

    expect(screen.getByText('至少添加 1 个域名')).toBeInTheDocument()
    expect(createPolicy).not.toHaveBeenCalled()
  })

  it('reverts optimistic toggle on failure', async () => {
    togglePolicy.mockRejectedValue(new Error('切换失败'))
    renderPage()
    await waitFor(() => expect(listPolicies).toHaveBeenCalled())

    const toggleButton = screen.getByRole('button', { name: '停用' })
    await userEvent.click(toggleButton)

    await waitFor(() => expect(togglePolicy).toHaveBeenCalledWith(1, false))
    expect(await screen.findByText('切换失败')).toBeInTheDocument()
    expect(toggleButton).toHaveTextContent('停用')
  })

  it('opens dry-run modal and calls service', async () => {
    dryRunPolicy.mockResolvedValue({ allowed: true, trace: [] })
    renderPage()
    await waitFor(() => expect(listPolicies).toHaveBeenCalled())

    await userEvent.click(screen.getByRole('button', { name: '测试' }))
    const modal = await screen.findByRole('dialog')
    await userEvent.clear(within(modal).getByLabelText('测试域名'))
    await userEvent.type(within(modal).getByLabelText('测试域名'), 'dryrun.example.com')
    await userEvent.click(within(modal).getByRole('button', { name: '执行测试' }))

    await waitFor(() => expect(dryRunPolicy).toHaveBeenCalledWith(1, { candidates: [{ domain: 'dryrun.example.com' }] }))
  })
})
