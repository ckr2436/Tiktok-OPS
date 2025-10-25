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
} from '../service.js'

vi.mock('../service.js', () => ({
  listPolicyProviders: vi.fn(),
  listPolicies: vi.fn(),
  createPolicy: vi.fn(),
  updatePolicy: vi.fn(),
  togglePolicy: vi.fn(),
  deletePolicy: vi.fn(),
}))

const providerFixtures = [
  { key: 'tiktok-business', name: 'TikTok Business', is_enabled: true },
]

const policyFixtures = [
  {
    id: 1,
    provider_key: 'tiktok-business',
    mode: 'WHITELIST',
    domain: 'api.example.com',
    is_enabled: true,
    description: 'Allow',
    created_at: '2025-01-01T00:00:00.000000',
    updated_at: '2025-01-01T00:00:00.000000',
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

  it('renders filters and table with data', async () => {
    renderPage()
    await waitFor(() => expect(listPolicies).toHaveBeenCalledTimes(1))

    expect(screen.getByLabelText('提供方')).toBeInTheDocument()
    const providerCells = screen.getAllByRole('cell', { name: 'TikTok Business' })
    expect(providerCells.length).toBeGreaterThan(0)
    expect(screen.getAllByRole('cell', { name: 'api.example.com' })[0]).toBeInTheDocument()
    expect(screen.getAllByRole('cell', { name: '白名单' })[0]).toBeInTheDocument()
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
    const providerSelect = within(modal).getByLabelText('提供方')
    await userEvent.selectOptions(providerSelect, 'tiktok-business')
    await userEvent.selectOptions(within(modal).getByLabelText('策略模式'), 'WHITELIST')
    const domainInput = within(modal).getByPlaceholderText('例如：api.example.com 或 *.example.com')
    await userEvent.clear(domainInput)
    await userEvent.type(domainInput, 'API.EXAMPLE.COM')
    await userEvent.type(within(modal).getByPlaceholderText('可选说明，帮助团队理解策略用途'), 'Created via test')
    const enabledCheckbox = within(modal).getByRole('checkbox', { name: '启用此策略' })
    expect(enabledCheckbox).toBeChecked()

    await userEvent.click(within(modal).getByRole('button', { name: '保存' }))

    await waitFor(() => {
      expect(createPolicy).toHaveBeenCalledWith({
        provider_key: 'tiktok-business',
        mode: 'WHITELIST',
        domain: 'api.example.com',
        description: 'Created via test',
        is_enabled: true,
      })
    })

    await waitFor(() => expect(listPolicies).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('策略已创建')).toBeInTheDocument()
  })

  it('shows validation error for invalid domain', async () => {
    listPolicies.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
    renderPage()
    await waitFor(() => expect(listPolicies).toHaveBeenCalled())

    await userEvent.click(screen.getByText('新建策略'))
    const modal = await screen.findByRole('dialog')
    const providerSelect = within(modal).getByLabelText('提供方')
    await userEvent.selectOptions(providerSelect, 'tiktok-business')
    const domainInput = within(modal).getByPlaceholderText('例如：api.example.com 或 *.example.com')
    await userEvent.clear(domainInput)
    await userEvent.type(domainInput, 'not-a-domain')

    await userEvent.click(within(modal).getByRole('button', { name: '保存' }))

    expect(screen.getByText('域名格式不正确，支持可选的 *. 前缀')).toBeInTheDocument()
    expect(createPolicy).not.toHaveBeenCalled()
  })

  it('reverts optimistic toggle on failure', async () => {
    togglePolicy.mockRejectedValue(new Error('切换失败'))
    renderPage()
    await waitFor(() => expect(listPolicies).toHaveBeenCalled())

    const toggleButton = screen.getByRole('button', { name: '停用' })
    await userEvent.click(toggleButton)

    await waitFor(() => expect(togglePolicy).toHaveBeenCalledWith(1, false))
    await waitFor(() => expect(toggleButton).toHaveTextContent('停用'))
    expect(screen.getByText('切换失败')).toBeInTheDocument()
  })
})
