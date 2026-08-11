import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import { VideoDiagnosisDrawer, VideoPlayerModal } from './TiktokShopVideoAnalyticsPage.jsx'

describe('TikTok Shop video player lifecycle', () => {
  afterEach(() => vi.restoreAllMocks())

  it('releases media resources and global listeners when unmounted', () => {
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    const load = vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
    const onClose = vi.fn()
    const view = render(
      <VideoPlayerModal
        playback={{
          video: { title: 'Test video', video_id: 'video-1', creator_username: 'creator' },
          presentation: { preview_url: '/video.mp4', poster_url: '/cover.jpg' },
        }}
        onClose={onClose}
      />,
    )

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    view.unmount()
    expect(pause).toHaveBeenCalledTimes(1)
    expect(load).toHaveBeenCalledTimes(1)

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('shows no spoken copy separately from missing transcript evidence and renders detailed analysis', () => {
    const onExport = vi.fn()
    const onOptimize = vi.fn()
    render(
      <MemoryRouter>
        <VideoDiagnosisDrawer
          video={{
            title: 'Gummy demo', video_id: 'video-1', currency: 'USD',
            diagnosis: { label: '可放大', tone: 'good', summary: '表现良好', recommendation: '继续测试', evidence: {} },
          }}
          presentation={{}}
          analysis={{
            status: 'SUCCEEDED', model: 'openai/gpt-5.4-mini',
            transcript: { status: 'NO_SPEECH', source: 'WHISPER_LOCAL', reason: 'NO_RELIABLE_SPEECH' },
            metrics: { shop: { views: 100, gmv: 20, currency: 'USD' }, paid: { available: true, product_impressions: 410, product_clicks: 38, view_rate_2s: 0.4462 } },
            analysis: {
              summary: '产品在首屏出现。', confidence: 0.8,
              hook_analysis: { first_2_seconds: '产品近景' },
              timeline: [{ time_or_cell: '0–2s', visual: '软糖特写', product_exposure: '清晰', purpose: '建立识别' }],
              strengths: [], problems: [], actions: [], limitations: [],
            },
          }}
          analyzing={false}
          workspaceId="3"
          onAnalyze={vi.fn()}
          onExport={onExport}
          onOptimize={onOptimize}
          onClose={vi.fn()}
          onPlay={vi.fn()}
        />
      </MemoryRouter>,
    )

    expect(screen.getByText('无口播文案')).toBeInTheDocument()
    expect(screen.getByText(/不生成虚构口播/)).toBeInTheDocument()
    expect(screen.getByText('前 2 秒钩子')).toBeInTheDocument()
    expect(screen.getByText('逐段内容拆解')).toBeInTheDocument()
    expect(screen.getByText('软糖特写')).toBeInTheDocument()
    expect(screen.getByText('410')).toBeInTheDocument()
    expect(screen.getByText('38')).toBeInTheDocument()
    expect(screen.getByText('44.6%')).toBeInTheDocument()
    expect(screen.getByText(/按商品广告曝光加权聚合/)).toBeInTheDocument()
    expect(screen.getByText(/不会自动创建项目或开始生成/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '导出分析报告' }))
    fireEvent.click(screen.getByRole('button', { name: '前往内容工厂优化' }))
    expect(onExport).toHaveBeenCalledTimes(1)
    expect(onOptimize).toHaveBeenCalledTimes(1)
  })
})
