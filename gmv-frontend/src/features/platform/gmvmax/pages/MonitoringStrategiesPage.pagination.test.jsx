import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../service.js', () => ({
  createMonitoringStrategy: vi.fn(),
  deleteMonitoringStrategy: vi.fn(),
  disableMonitoringStrategy: vi.fn(),
  enableMonitoringStrategy: vi.fn(),
  listMonitoringStrategies: vi.fn(),
  updateMonitoringStrategy: vi.fn(),
}))

import { listMonitoringStrategies } from '../service.js'
import MonitoringStrategiesPage from './MonitoringStrategiesPage.jsx'


function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MonitoringStrategiesPage />
    </QueryClientProvider>,
  )
}


describe('MonitoringStrategiesPage server pagination', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    listMonitoringStrategies.mockImplementation(async (params) => ({
      items: [
        {
          id: params.page,
          workspace_id: params.workspace_id || 7,
          category: 'GMVMAX',
          task_name: 'gmvmax.strategy',
          interval_minutes: 10,
          enabled: true,
        },
      ],
      page: params.page,
      page_size: params.page_size,
      total: 105,
    }))
  })

  it('requests every selected server page and resets to page one on filtering', async () => {
    renderPage()

    await waitFor(() => {
      expect(listMonitoringStrategies).toHaveBeenLastCalledWith({
        page: 1,
        page_size: 50,
      })
    })
    expect(await screen.findByText('第 1 / 3 页')).toBeInTheDocument()
    expect(screen.getByText('共 105 条记录')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await waitFor(() => {
      expect(listMonitoringStrategies).toHaveBeenLastCalledWith({
        page: 2,
        page_size: 50,
      })
    })
    expect(await screen.findByText('第 2 / 3 页')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('workspace_id'), { target: { value: '9' } })
    await waitFor(() => {
      expect(listMonitoringStrategies).toHaveBeenLastCalledWith({
        page: 1,
        page_size: 50,
        workspace_id: 9,
      })
    })
    expect(await screen.findByText('第 1 / 3 页')).toBeInTheDocument()
  })
})
