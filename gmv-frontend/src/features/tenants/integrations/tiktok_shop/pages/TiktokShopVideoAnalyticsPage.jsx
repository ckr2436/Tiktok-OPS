import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'

import Loading from '@/components/ui/Loading.jsx'
import { getCommerceContext } from '@/features/tenants/commerce/api.js'
import {
  errorMessage,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  formatRatio,
  rangeForPreset,
  rangeLabel,
  shortText,
} from '@/features/tenants/commerce/commerceUtils.js'
import {
  getAllTikTokShopProducts,
  getAllTikTokShopAnalytics,
  getTikTokShopAnalytics,
  getTikTokShopGuardFeed,
  lookupTikTokShopVideoMedia,
  lookupTikTokShopVideoAnalyses,
  requestTikTokShopVideoAnalysis,
} from '../service.js'
import {
  buildProductMatrix,
  buildDailyVideoSeries,
  dataTrustMeta,
  diagnoseVideos,
  filterAndSortVideos,
  formatVideoPublishedAt,
  nextDate,
  summarizeDiagnoses,
  summarizeOverviewDay,
  summarizeVideoAnalytics,
} from '../videoAnalyticsUtils.js'
import {
  buildProductMap,
  buildVideoMediaMap,
  resolveVideoPresentation,
} from '../videoMediaUtils.js'
import '@/features/tenants/commerce/commerce.css'
import './videoAnalytics.css'

const RANGE_OPTIONS = [
  { value: 'today', label: '今日' },
  { value: 'yesterday', label: '昨日' },
  { value: '7d', label: '近 7 日' },
  { value: '30d', label: '近 30 日' },
  { value: 'custom', label: '自定义' },
]

const SORT_OPTIONS = [
  { value: 'diagnosis_priority', label: '按运营优先级排序' },
  { value: 'gmv', label: '按 GMV 排序' },
  { value: 'views', label: '按播放量排序' },
  { value: 'sku_orders', label: '按订单排序' },
  { value: 'gpm', label: '按千次播放产出排序' },
  { value: 'click_through_rate', label: '按点击率排序' },
]

const WORKSPACE_TABS = [
  { value: 'today', label: '经营概览' },
  { value: 'videos', label: '内容雷达' },
  { value: 'products', label: '商品内容矩阵' },
  { value: 'guard', label: '投放与守护' },
  { value: 'review', label: '策略复盘' },
]

const PAGE_SIZE = 20

function MetricCard({ label, value, detail, tone = '' }) {
  return (
    <article className={`shop-video-kpi ${tone ? `shop-video-kpi--${tone}` : ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  )
}

function overviewSourceLabel(day) {
  if (!day?.available) return '尚未同步'
  if (day.data_source === 'shop_and_product_video_channels') return '官方字段临时回补'
  return day.is_provisional ? '官方实时概览' : '官方已结算概览'
}

function DayOverviewCard({ title, day, stable = false }) {
  return (
    <article className={`shop-video-day-card ${stable ? 'is-stable' : 'is-live'}`}>
      <header>
        <div><span>{title}</span><strong>{day.report_date || '--'}</strong></div>
        <TrustBadge tone={day.available ? (stable ? 'official' : 'calculated') : 'muted'}>
          {overviewSourceLabel(day)}
        </TrustBadge>
      </header>
      {day.available ? (
        <div className="shop-video-day-card__metrics">
          <span><small>视频 GMV</small><strong>{formatMoney(day.gmv, day.currency)}</strong></span>
          <span><small>SKU 订单</small><strong>{formatNumber(day.sku_orders)}</strong></span>
          <span><small>商品曝光</small><strong>{formatNumber(day.product_impressions)}</strong></span>
          <span><small>商品点击</small><strong>{formatNumber(day.product_clicks)}</strong></span>
          <span><small>官方视频点击率</small><strong>{formatPercent(day.click_through_rate)}</strong></span>
        </div>
      ) : <div className="commerce-empty">该自然日尚无视频概览。</div>}
      <footer>
        <span>{stable ? '历史日数据固化保存' : '当天数据持续变化，次日重新核准'}</span>
        <span>同步 {formatDateTime(day.synced_at)}</span>
      </footer>
    </article>
  )
}

function ProductThumbnailList({ products = [], productMap }) {
  const linked = (Array.isArray(products) ? products : [])
    .map((product) => {
      const productId = String(product?.id || product?.product_id || '')
      const systemProduct = productMap.get(productId) || null
      return {
        productId,
        title: systemProduct?.title || product?.name || productId,
        imageUrl: systemProduct?.main_image_url || null,
      }
    })
    .filter((product) => product.productId)
  if (!linked.length) return <span className="shop-video-product-empty">--</span>
  return (
    <div className="shop-video-product-thumbs">
      {linked.slice(0, 3).map((product) => (
        <span key={product.productId} title={`${product.title} · ${product.productId}`}>
          {product.imageUrl
            ? <img src={product.imageUrl} alt={product.title || '系统商品'} loading="lazy" />
            : <i aria-label="系统商品暂无缩略图">商</i>}
        </span>
      ))}
      {linked.length > 3 && <small>+{linked.length - 3}</small>}
    </div>
  )
}

function VideoTrendChart({ rows, currency }) {
  if (!rows.length) return <div className="commerce-empty">所选日期暂无视频趋势。</div>
  const width = 960
  const height = 270
  const padding = { left: 58, right: 58, top: 24, bottom: 40 }
  const chartWidth = width - padding.left - padding.right
  const chartHeight = height - padding.top - padding.bottom
  const maxGmv = Math.max(1, ...rows.map((item) => Number(item.gmv || 0)))
  const maxViews = Math.max(1, ...rows.map((item) => Number(item.views || 0)))
  const x = (index) => padding.left + (
    rows.length === 1 ? chartWidth / 2 : (index / (rows.length - 1)) * chartWidth
  )
  const yGmv = (value) => padding.top + chartHeight - (Number(value || 0) / maxGmv) * chartHeight
  const yViews = (value) => padding.top + chartHeight - (Number(value || 0) / maxViews) * chartHeight
  const path = (key, scale) => rows
    .map((item, index) => `${index === 0 ? 'M' : 'L'} ${x(index)} ${scale(item[key])}`)
    .join(' ')
  const labels = [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])]

  return (
    <div className="shop-video-chart">
      <div className="shop-video-chart__legend">
        <span><i className="shop-video-dot shop-video-dot--gmv" />视频 GMV</span>
        <span><i className="shop-video-dot shop-video-dot--views" />播放量</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="短视频 GMV 与播放量趋势">
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
          const gridY = padding.top + chartHeight * ratio
          return (
            <g key={ratio}>
              <line x1={padding.left} x2={width - padding.right} y1={gridY} y2={gridY} className="shop-video-chart__grid" />
              <text x={padding.left - 9} y={gridY + 4} textAnchor="end">
                {formatMoney(maxGmv * (1 - ratio), currency).replace(/\.\d{2}$/, '')}
              </text>
              <text x={width - padding.right + 9} y={gridY + 4} textAnchor="start">
                {formatNumber(maxViews * (1 - ratio))}
              </text>
            </g>
          )
        })}
        <path d={path('gmv', yGmv)} className="shop-video-chart__line shop-video-chart__line--gmv" />
        <path d={path('views', yViews)} className="shop-video-chart__line shop-video-chart__line--views" />
        {rows.map((item, index) => (
          <g key={item.date}>
            <circle cx={x(index)} cy={yGmv(item.gmv)} r="3.5" className="shop-video-chart__point shop-video-chart__point--gmv">
              <title>{`${item.date} 视频 GMV ${formatMoney(item.gmv, currency)}`}</title>
            </circle>
            <circle cx={x(index)} cy={yViews(item.views)} r="3.5" className="shop-video-chart__point shop-video-chart__point--views">
              <title>{`${item.date} 播放量 ${formatNumber(item.views)}`}</title>
            </circle>
          </g>
        ))}
        {labels.map((index) => (
          <text
            key={rows[index].date}
            x={x(index)}
            y={height - 12}
            textAnchor={index === 0 ? 'start' : index === rows.length - 1 ? 'end' : 'middle'}
          >
            {rows[index].date.slice(5)}
          </text>
        ))}
      </svg>
    </div>
  )
}

function formatDuration(value) {
  const seconds = Number(value)
  if (!Number.isFinite(seconds) || seconds <= 0) return '--'
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.round(seconds % 60)
  return minutes ? `${minutes}:${String(remainder).padStart(2, '0')}` : `${remainder} 秒`
}

function creatorType(value) {
  const normalized = String(value || '').toUpperCase()
  if (normalized.includes('AFFILIATE')) return '达人视频'
  if (normalized.includes('SHOP') || normalized.includes('SELLER')) return '店铺自营'
  return value || '未分类'
}

function VideoThumbnail({ video, presentation, onPlay }) {
  const [imageFailed, setImageFailed] = useState(false)

  useEffect(() => setImageFailed(false), [presentation.poster_url])

  const media = (
    <>
      {presentation.poster_url && !imageFailed ? (
        <img
          src={presentation.poster_url}
          alt={presentation.poster_kind === 'VIDEO_COVER' ? `${video.title} 视频封面` : `${video.title} 关联商品图`}
          loading="lazy"
          onError={() => setImageFailed(true)}
        />
      ) : (
        <span className="shop-video-thumb__placeholder" aria-hidden="true">▶</span>
      )}
      {presentation.preview_url && <span className="shop-video-thumb__play" aria-hidden="true">▶</span>}
      {presentation.poster_kind === 'PRODUCT_IMAGE' && <small>商品图</small>}
    </>
  )

  return presentation.preview_url ? (
    <button
      className="shop-video-thumb"
      type="button"
      onClick={onPlay}
      aria-label={`播放 ${video.title}`}
    >
      {media}
    </button>
  ) : <div className="shop-video-thumb">{media}</div>
}

export function VideoPlayerModal({ playback, onClose }) {
  const videoRef = useRef(null)
  useEffect(() => {
    if (!playback) return undefined
    const player = videoRef.current
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => {
      window.removeEventListener('keydown', handleKeyDown)
      if (player) {
        player.pause()
        player.removeAttribute('src')
        player.load()
      }
    }
  }, [onClose, playback])

  if (!playback) return null
  const { video, presentation } = playback
  return (
    <div className="shop-video-player" role="dialog" aria-modal="true" aria-label={`${video.title} 视频播放`} onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <div className="shop-video-player__card">
        <header>
          <div>
            <strong>{video.title}</strong>
            <small>ID {video.video_id} · @{video.creator_username || '未知'}</small>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭视频播放器">×</button>
        </header>
        <video
          ref={videoRef}
          src={presentation.preview_url}
          poster={presentation.poster_url || undefined}
          controls
          autoPlay
          playsInline
          preload="metadata"
        >
          当前浏览器不支持视频播放。
        </video>
      </div>
    </div>
  )
}

function DiagnosisBadge({ diagnosis }) {
  const item = diagnosis || { label: '数据不足', tone: 'muted' }
  return <span className={`shop-ops-diagnosis is-${item.tone}`}>{item.label}</span>
}

function TrustBadge({ children, tone = 'neutral' }) {
  return <span className={`shop-ops-trust-badge is-${tone}`}>{children}</span>
}

function ActionCard({ video, execution, onOpen }) {
  return (
    <button className="shop-ops-action-card" type="button" onClick={() => onOpen(video)}>
      <span className={`shop-ops-action-card__signal is-${video.diagnosis.tone}`} aria-hidden="true" />
      <span className="shop-ops-action-card__body">
        <span><DiagnosisBadge diagnosis={video.diagnosis} /><small>视频 {video.video_id}</small></span>
        <strong>{shortText(video.title, 52)}</strong>
        <small>{video.diagnosis.summary}</small>
      </span>
      <span className="shop-ops-action-card__metric">
        <strong>{formatMoney(video.gmv, video.currency)}</strong>
        <small>{execution ? `最近执行：${execution.result || execution.action}` : '查看依据 →'}</small>
      </span>
    </button>
  )
}

function transcriptTime(value) {
  const seconds = Math.max(0, Number(value) || 0)
  const minutes = Math.floor(seconds / 60)
  return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`
}

const DETAIL_LABELS = {
  hook: '钩子', format: '内容形式', audience: '目标受众', product_visibility: '商品可见度',
  pacing: '整体节奏', cta: '行动号召', production_style: '制作风格',
  first_2_seconds: '前2秒', spoken_hook: '口播钩子', visual_hook: '画面钩子', promise: '核心承诺', risk: '风险',
  products: '关联商品', first_reveal_time: '首次露出', demonstration: '产品演示', proof: '证明方式', offer_handoff: '购买承接',
  status: '状态', structure: '文案结构', selling_points: '核心卖点', objections: '异议处理', claims: '表达主张', clarity: '清晰度',
  opening: '开场', middle: '中段', ending: '结尾', likely_drop_points: '可能掉点', edit_rhythm: '剪辑节奏',
  spoken_cta: '口播CTA', visual_cta: '画面CTA', timing: '出现时机', friction: '转化阻力',
  hypothesis: '实验假设', change_one_variable: '单一变量', control: '对照版本', success_metric: '成功指标',
}

function DetailFacts({ title, data }) {
  const entries = Object.entries(data || {}).filter(([, value]) => value != null && String(value).trim())
  if (!entries.length) return null
  return (
    <section className="shop-hermes-detail-card">
      <h4>{title}</h4>
      <dl>{entries.map(([key, value]) => <div key={key}><dt>{DETAIL_LABELS[key] || key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>)}</dl>
    </section>
  )
}

function TranscriptPanel({ transcript }) {
  const status = String(transcript?.status || 'PENDING').toUpperCase()
  const labels = {
    PENDING: '等待 Whisper',
    PROCESSING: 'Whisper 提取中',
    READY: '字幕已提取',
    NO_SPEECH: '无口播文案',
    FAILED: '字幕提取失败',
    UNAVAILABLE: '无本地音频证据',
  }
  const segments = Array.isArray(transcript?.segments) ? transcript.segments : []
  return (
    <section className="shop-hermes-transcript">
      <header>
        <div><h4>口播与字幕</h4><small>本地 Whisper · {transcript?.language || '语言待识别'}</small></div>
        <TrustBadge tone={status === 'READY' ? 'official' : status === 'NO_SPEECH' ? 'neutral' : 'calculated'}>{labels[status] || status}</TrustBadge>
      </header>
      {status === 'NO_SPEECH' && <p>未检测到可靠人声文案；按纯音乐、环境声或无人声内容处理，不生成虚构口播。</p>}
      {status === 'FAILED' && <p>Whisper 未能完成提取。本次 Hermes 只依据画面与指标分析，不能据此判断视频没有口播。</p>}
      {status === 'UNAVAILABLE' && <p>本地没有可读取的视频音轨，本次仅分析封面/关键帧与经营指标。</p>}
      {['PENDING', 'PROCESSING'].includes(status) && <p>正在读取本地视频音轨；字幕完成后会自动进入 Hermes 内容分析。</p>}
      {status === 'READY' && segments.length > 0 && (
        <ol>{segments.map((segment, index) => (
          <li key={`${segment.index ?? index}-${segment.start}`}>
            <time>{transcriptTime(segment.start)}–{transcriptTime(segment.end)}</time>
            <span>{segment.text}</span>
          </li>
        ))}</ol>
      )}
    </section>
  )
}

export function VideoDiagnosisDrawer({
  video,
  presentation,
  execution,
  analysis,
  analysisError,
  analyzing,
  workspaceId,
  onAnalyze,
  onClose,
  onPlay,
}) {
  useEffect(() => {
    if (!video) return undefined
    const handleKeyDown = (event) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose, video])

  if (!video) return null
  const evidence = video.diagnosis?.evidence || {}
  const hermes = analysis?.analysis || null
  const paid = analysis?.metrics?.paid || null
  const shopMetrics = analysis?.metrics?.shop || null
  const transcript = analysis?.transcript || null
  const transcriptStatus = String(transcript?.status || '').toUpperCase()
  const pending = ['QUEUED', 'RUNNING'].includes(String(analysis?.status || '').toUpperCase())
  const statusLabel = {
    QUEUED: ['PENDING', 'PROCESSING'].includes(transcriptStatus) ? '字幕提取中' : '排队中',
    RUNNING: '分析中',
    SUCCEEDED: '分析完成',
    FAILED: '分析失败',
    UNAVAILABLE: '媒体不可用',
  }[String(analysis?.status || '').toUpperCase()] || '尚未分析'
  return (
    <div className="shop-ops-drawer-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <aside className="shop-ops-drawer" role="dialog" aria-modal="true" aria-label={`${video.title} 运营诊断`}>
        <header className="shop-ops-drawer__header">
          <div><span>视频运营诊断</span><h2>{video.title}</h2><small>ID {video.video_id}</small></div>
          <button type="button" onClick={onClose} aria-label="关闭诊断详情">×</button>
        </header>
        <div className="shop-ops-drawer__hero">
          <VideoThumbnail video={video} presentation={presentation} onPlay={onPlay} />
          <div>
            <DiagnosisBadge diagnosis={video.diagnosis} />
            <strong>{video.diagnosis?.summary}</strong>
            <p>{video.diagnosis?.recommendation}</p>
          </div>
        </div>
        <section className="shop-ops-evidence">
          <h3>判断依据</h3>
          <div>
            <span><small>播放量</small><strong>{formatNumber(evidence.views)}</strong><em>店铺中位 {formatNumber(evidence.views_median)}</em></span>
            <span><small>千次播放产出</small><strong>{formatMoney(evidence.gpm, video.currency)}</strong><em>店铺中位 {formatMoney(evidence.gpm_median, video.currency)}</em></span>
            <span><small>商品点击率</small><strong>{formatPercent(evidence.click_through_rate)}</strong><em>店铺中位 {formatPercent(evidence.ctr_median)}</em></span>
            <span><small>周期变化</small><strong>{evidence.period_change == null ? '--' : formatPercent(evidence.period_change)}</strong><em>{evidence.report_days || 0} 个统计日</em></span>
          </div>
        </section>
        <section className="shop-ops-explain">
          <h3>数据边界</h3>
          <p>诊断使用TikTok Shop视频级官方数据和店铺内中位数比较。关联商品只说明内容关系，不把视频GMV强行分摊给单个商品。</p>
          <div><TrustBadge tone="official">官方数据</TrustBadge><TrustBadge tone="calculated">系统计算</TrustBadge><TrustBadge tone="advice">规则建议</TrustBadge></div>
        </section>
        <section className="shop-hermes-analysis">
          <div className="shop-hermes-analysis__header">
            <div>
              <span className="shop-video-eyebrow">HERMES VIDEO ANALYST</span>
              <h3>视频内容与投放漏斗分析</h3>
              <small>{statusLabel}{analysis?.model ? ` · ${analysis.model}` : ''}</small>
            </div>
            <button
              className="btn"
              type="button"
              disabled={analyzing || pending}
              onClick={onAnalyze}
            >
              {analyzing || pending ? '分析中…' : analysis ? '重新检查' : '开始分析'}
            </button>
          </div>
          {analysisError && <div className="alert alert--error">{errorMessage(analysisError)}</div>}
          {analysis?.error && (
            <div className="shop-hermes-analysis__notice">
              <strong>{analysis.error.code}</strong>
              <span>{analysis.error.message}</span>
            </div>
          )}
          {analysis && <TranscriptPanel transcript={transcript} />}
          {(shopMetrics || paid) && (
            <div className="shop-hermes-metric-groups">
              {shopMetrics && <section><header><h4>Shop 内容经营</h4><TrustBadge tone="official">官方数据</TrustBadge></header><div>
                <span><small>播放量</small><strong>{formatNumber(shopMetrics.views)}</strong></span>
                <span><small>视频 GMV</small><strong>{formatMoney(shopMetrics.gmv, shopMetrics.currency || video.currency)}</strong></span>
                <span><small>千次播放产出</small><strong>{formatMoney(shopMetrics.gpm, shopMetrics.currency || video.currency)}</strong></span>
                <span><small>商品点击率</small><strong>{formatPercent(shopMetrics.click_through_rate)}</strong></span>
                <span><small>SKU 订单</small><strong>{formatNumber(shopMetrics.sku_orders)}</strong></span>
                <span><small>售出件数</small><strong>{formatNumber(shopMetrics.items_sold)}</strong></span>
              </div></section>}
              {paid?.available && <section><header><h4>GMV Max 投放</h4><TrustBadge tone="official">官方数据</TrustBadge></header><div>
                <span><small>商品广告曝光</small><strong>{formatNumber(paid.product_impressions)}</strong></span>
                <span><small>商品广告点击</small><strong>{formatNumber(paid.product_clicks)}</strong></span>
                <span><small>消耗</small><strong>{formatMoney(paid.cost, shopMetrics?.currency || video.currency)}</strong></span>
                <span><small>广告成交</small><strong>{formatMoney(paid.gross_revenue, shopMetrics?.currency || video.currency)}</strong></span>
                <span><small>ROI</small><strong>{formatRatio(paid.roi)}</strong></span>
                <span><small>转化率</small><strong>{formatPercent(paid.conversion_rate)}</strong></span>
              </div></section>}
            </div>
          )}
          {paid?.available && (
            <><div className="shop-hermes-funnel" aria-label="GMV Max 视频播放漏斗">
              <span><small>2秒播放率</small><strong>{formatPercent(paid.view_rate_2s)}</strong></span>
              <span><small>6秒播放率</small><strong>{formatPercent(paid.view_rate_6s)}</strong></span>
              <span><small>25%播放</small><strong>{formatPercent(paid.view_rate_25)}</strong></span>
              <span><small>50%播放</small><strong>{formatPercent(paid.view_rate_50)}</strong></span>
              <span><small>75%播放</small><strong>{formatPercent(paid.view_rate_75)}</strong></span>
              <span><small>完整播放</small><strong>{formatPercent(paid.view_rate_100)}</strong></span>
            </div><small className="shop-hermes-funnel-note">官方日级播放率 · 系统按商品广告曝光加权聚合</small>
            {paid.rate_quality_flags?.includes('OFFICIAL_COMPLETION_FUNNEL_NON_MONOTONIC') && <small className="shop-hermes-funnel-warning">官方原始日报存在完播漏斗倒挂，系统保留原值并提示，不做人工修正。</small>}</>
          )}
          {paid && !paid.available && (
            <p className="shop-hermes-analysis__empty">所选周期没有匹配到 GMV Max 创意播放指标；这代表未观测到，不按 0 处理。</p>
          )}
          {hermes ? (
            <div className="shop-hermes-analysis__result">
              <div className="shop-hermes-summary"><TrustBadge tone="advice">Hermes 结论</TrustBadge><p>{hermes.summary}</p><small>置信度 {formatPercent(hermes.confidence)}</small></div>
              <div className="shop-hermes-detail-grid">
                <DetailFacts title="内容定位" data={hermes.content_profile} />
                <DetailFacts title="前 2 秒钩子" data={hermes.hook_analysis} />
                <DetailFacts title="商品露出与证明" data={hermes.product_analysis} />
                <DetailFacts title="口播文案结构" data={hermes.spoken_copy_analysis} />
                <DetailFacts title="节奏与掉点" data={hermes.pacing_analysis} />
                <DetailFacts title="行动号召" data={hermes.cta_analysis} />
              </div>
              {!!hermes.timeline?.length && <section className="shop-hermes-timeline"><h4>逐段内容拆解</h4>{hermes.timeline.map((item, index) => <article key={`timeline-${index}`}><time>{item.time_or_cell || `片段 ${index + 1}`}</time><div><strong>{item.visual || item.observation || '画面证据'}</strong>{item.spoken_copy && <p>口播：{item.spoken_copy}</p>}{item.product_exposure && <p>商品：{item.product_exposure}</p>}<small>{item.purpose || item.operational_meaning}</small></div></article>)}</section>}
              {!!hermes.strengths?.length && <div><h4>有效做法</h4><ul>{hermes.strengths.map((item, index) => <li key={`strength-${index}`}>{String(item)}</li>)}</ul></div>}
              {!!hermes.problems?.length && <div><h4>主要问题与证据</h4><ul>{hermes.problems.map((item, index) => <li key={`problem-${index}`}><strong>{item.issue || item.severity}</strong>{item.visual_evidence ? ` · 画面：${item.visual_evidence}` : ''}{item.metric_evidence ? ` · 数据：${item.metric_evidence}` : ''}{item.why_it_matters ? ` · 影响：${item.why_it_matters}` : ''}</li>)}</ul></div>}
              {!!hermes.actions?.length && <div><h4>下一步动作</h4><ol>{hermes.actions.map((item, index) => <li key={`action-${index}`}><strong>{item.priority ? `${item.priority} · ` : ''}{item.action || `动作 ${index + 1}`}</strong>{item.expected_metric ? ` · 观察 ${item.expected_metric}` : ''}{item.validation_window ? ` · ${item.validation_window}` : ''}</li>)}</ol></div>}
              <DetailFacts title="下一轮单变量实验" data={hermes.next_experiment} />
              {!!hermes.limitations?.length && <small>证据限制：{hermes.limitations.join('；')}</small>}
            </div>
          ) : !pending && !analysis?.error ? (
            <p className="shop-hermes-analysis__empty">Hermes 尚未读取该视频。点击开始后，系统会组合本地 Whisper 字幕、关键帧、Shop经营数据和GMV Max播放漏斗；不会上传整段视频。</p>
          ) : null}
        </section>
        {execution && (
          <section className="shop-ops-explain">
            <h3>最近一次关联执行</h3>
            <p>{execution.action} · {execution.result || '已记录'} · {formatDateTime(execution.created_at)}</p>
            <small>{execution.reason || '系统未记录额外原因。'}</small>
          </section>
        )}
        <footer className="shop-ops-drawer__actions">
          {presentation?.preview_url && <button className="btn ghost" type="button" onClick={onPlay}>播放视频</button>}
          <Link className="btn" to={`/tenants/${workspaceId}/gmvmax`}>前往 GMV Max 执行</Link>
        </footer>
      </aside>
    </div>
  )
}

function GuardTimeline({ items = [], workspaceId }) {
  if (!items.length) return <div className="commerce-empty">当前店铺暂无Smart Guard执行记录。</div>
  return (
    <div className="shop-ops-timeline">
      {items.map((item) => {
        const failed = String(item.result || '').toUpperCase().includes('FAIL') || item.error_message
        return (
          <article key={item.id} className={`shop-ops-timeline__item ${failed ? 'is-failed' : ''}`}>
            <span className="shop-ops-timeline__dot" aria-hidden="true" />
            <div>
              <header>
                <strong>{item.action || item.event_type || '守护检查'}</strong>
                <TrustBadge tone={failed ? 'danger' : 'official'}>{item.result || '已记录'}</TrustBadge>
                <time>{formatDateTime(item.created_at)}</time>
              </header>
              <p>{item.reason || '系统未记录额外原因。'}</p>
              <small>广告系列 {item.campaign_id} · {item.operator || '系统'}{item.official_request_id ? ` · 官方请求 ${item.official_request_id}` : ''}</small>
              {item.error_message && <em>{item.error_message}</em>}
              <Link to={`/tenants/${workspaceId}/gmvmax`}>查看投放详情 →</Link>
            </div>
          </article>
        )
      })}
    </div>
  )
}

export default function TiktokShopVideoAnalyticsPage() {
  const { wid } = useParams()
  const [activeTab, setActiveTab] = useState('today')
  const [shopId, setShopId] = useState('')
  const [preset, setPreset] = useState('today')
  const [customRange, setCustomRange] = useState({ start_date: '', end_date: '' })
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState('diagnosis_priority')
  const [page, setPage] = useState(1)
  const [productPage, setProductPage] = useState(1)
  const [playback, setPlayback] = useState(null)
  const [detailVideoId, setDetailVideoId] = useState(null)

  const contextQuery = useQuery({
    queryKey: ['commerce', 'context', wid],
    queryFn: ({ signal }) => getCommerceContext(wid, { signal }),
    enabled: Boolean(wid),
    staleTime: 5 * 60 * 1000,
  })
  const context = contextQuery.data || {}
  const shops = Array.isArray(context.shops) ? context.shops : []

  useEffect(() => {
    if (!shops.length) return
    setShopId((current) => (
      shops.some((shop) => String(shop.id) === String(current))
        ? current
        : String(context.default_shop_id || shops[0].id)
    ))
  }, [context.default_shop_id, shops])

  const selectedShop = shops.find((shop) => String(shop.id) === String(shopId))
  const timezone = selectedShop?.timezone || 'Etc/GMT+8'
  const selectedRange = useMemo(
    () => (preset === 'custom' ? customRange : rangeForPreset(preset, timezone)),
    [customRange, preset, timezone],
  )
  const todayRange = useMemo(() => rangeForPreset('today', timezone), [timezone])
  const yesterdayRange = useMemo(() => rangeForPreset('yesterday', timezone), [timezone])
  const validRange = Boolean(
    selectedRange.start_date
    && selectedRange.end_date
    && selectedRange.start_date <= selectedRange.end_date,
  )
  const analyticsParams = useMemo(() => ({
    shop_id: shopId || undefined,
    start_date: selectedRange.start_date || undefined,
    end_date_exclusive: nextDate(selectedRange.end_date) || undefined,
  }), [selectedRange.end_date, selectedRange.start_date, shopId])

  const overviewQuery = useQuery({
    queryKey: ['tiktok-shop', 'video-overview', wid, analyticsParams],
    queryFn: ({ signal }) => getTikTokShopAnalytics(
      wid,
      'video-overview',
      { ...analyticsParams, page: 1, page_size: 366 },
      { signal },
    ),
    enabled: Boolean(wid && shopId && validRange),
    staleTime: 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
  })
  const comparisonParams = useMemo(() => ({
    shop_id: shopId || undefined,
    start_date: yesterdayRange.start_date,
    end_date_exclusive: nextDate(todayRange.end_date),
    page: 1,
    page_size: 2,
  }), [shopId, todayRange.end_date, yesterdayRange.start_date])
  const comparisonOverviewQuery = useQuery({
    queryKey: ['tiktok-shop', 'video-overview-comparison', wid, comparisonParams],
    queryFn: ({ signal }) => getTikTokShopAnalytics(
      wid,
      'video-overview',
      comparisonParams,
      { signal },
    ),
    enabled: Boolean(wid && shopId),
    staleTime: 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
    refetchIntervalInBackground: false,
  })
  const videosQuery = useQuery({
    queryKey: ['tiktok-shop', 'videos', wid, analyticsParams],
    queryFn: ({ signal }) => getAllTikTokShopAnalytics(
      wid,
      'videos',
      analyticsParams,
      { signal },
    ),
    enabled: Boolean(wid && shopId && validRange),
    staleTime: 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
  })

  const overviewRows = overviewQuery.data?.items || []
  const videoRows = videosQuery.data?.items || []
  const summary = useMemo(
    () => summarizeVideoAnalytics(videoRows, overviewRows),
    [overviewRows, videoRows],
  )
  const comparisonRows = comparisonOverviewQuery.data?.items || []
  const todayOverview = useMemo(
    () => summarizeOverviewDay(comparisonRows, todayRange.end_date),
    [comparisonRows, todayRange.end_date],
  )
  const yesterdayOverview = useMemo(
    () => summarizeOverviewDay(comparisonRows, yesterdayRange.end_date),
    [comparisonRows, yesterdayRange.end_date],
  )
  const videoIds = useMemo(
    () => summary.videos.map((video) => video.video_id).filter(Boolean),
    [summary.videos],
  )
  const mediaQuery = useQuery({
    queryKey: ['tiktok-shop', 'video-media', wid, shopId, videoIds.join('|')],
    queryFn: ({ signal }) => lookupTikTokShopVideoMedia(wid, shopId, videoIds, { signal }),
    enabled: Boolean(wid && shopId && videoIds.length),
    staleTime: 5 * 60 * 1000,
  })
  const productsQuery = useQuery({
    queryKey: ['tiktok-shop', 'product-catalog', wid, shopId],
    queryFn: ({ signal }) => getAllTikTokShopProducts(wid, shopId, { signal }),
    enabled: Boolean(wid && shopId),
    staleTime: 5 * 60 * 1000,
    gcTime: 15 * 60 * 1000,
  })
  const productMetricsQuery = useQuery({
    queryKey: ['tiktok-shop', 'product-metrics', wid, analyticsParams],
    queryFn: ({ signal }) => getAllTikTokShopAnalytics(
      wid,
      'products',
      analyticsParams,
      { signal },
    ),
    enabled: Boolean(wid && shopId && validRange),
    staleTime: 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
    refetchIntervalInBackground: false,
  })
  const guardQuery = useQuery({
    queryKey: ['tiktok-shop', 'guard-feed', wid, shopId],
    queryFn: ({ signal }) => getTikTokShopGuardFeed(wid, shopId, { limit: 60 }, { signal }),
    enabled: Boolean(wid && shopId),
    staleTime: 30 * 1000,
    refetchInterval: () => (document.visibilityState === 'visible' ? 60 * 1000 : false),
    refetchIntervalInBackground: false,
  })
  const analysisQuery = useQuery({
    queryKey: ['tiktok-shop', 'video-analysis', wid, shopId, detailVideoId, analyticsParams.start_date, analyticsParams.end_date_exclusive],
    queryFn: ({ signal }) => lookupTikTokShopVideoAnalyses(
      wid,
      shopId,
      [detailVideoId],
      analyticsParams,
      { signal },
    ),
    enabled: Boolean(wid && shopId && detailVideoId && validRange),
    staleTime: 15 * 1000,
    refetchInterval: (query) => {
      const status = query.state.data?.items?.[0]?.status
      return document.visibilityState === 'visible' && ['QUEUED', 'RUNNING'].includes(status)
        ? 3000
        : false
    },
    refetchIntervalInBackground: false,
  })
  const analysisMutation = useMutation({
    mutationFn: () => requestTikTokShopVideoAnalysis(wid, {
      shop_id: Number(shopId),
      video_id: detailVideoId,
      start_date: analyticsParams.start_date,
      end_date_exclusive: analyticsParams.end_date_exclusive,
      retry_failed: ['FAILED', 'UNAVAILABLE'].includes(analysisQuery.data?.items?.[0]?.status),
    }),
    onSuccess: () => analysisQuery.refetch(),
  })
  const mediaMap = useMemo(
    () => buildVideoMediaMap(mediaQuery.data?.items),
    [mediaQuery.data?.items],
  )
  const productMap = useMemo(
    () => buildProductMap(productsQuery.data?.items),
    [productsQuery.data?.items],
  )
  const trend = useMemo(
    () => buildDailyVideoSeries(videoRows, overviewRows),
    [overviewRows, videoRows],
  )
  const diagnosedVideos = useMemo(() => diagnoseVideos(videoRows), [videoRows])
  const diagnosisSummary = useMemo(
    () => summarizeDiagnoses(diagnosedVideos),
    [diagnosedVideos],
  )
  const executionByVideo = useMemo(() => {
    const mapping = new Map()
    for (const item of Array.isArray(guardQuery.data?.items) ? guardQuery.data.items : []) {
      const videoId = String(item?.creative_id || '')
      if (videoId && !mapping.has(videoId)) mapping.set(videoId, item)
    }
    return mapping
  }, [guardQuery.data?.items])
  const productMatrix = useMemo(
    () => buildProductMatrix(
      productMetricsQuery.data?.items,
      productsQuery.data?.items,
      diagnosedVideos,
    ),
    [diagnosedVideos, productMetricsQuery.data?.items, productsQuery.data?.items],
  )
  const trustMeta = useMemo(() => dataTrustMeta({
    endDate: selectedRange.end_date,
    latestReportDate: summary.latest_report_date,
    today: rangeForPreset('today', timezone).end_date,
    syncedAt: summary.latest_synced_at,
    rowCount: videoRows.length,
    total: videosQuery.data?.total || 0,
  }), [selectedRange.end_date, summary.latest_report_date, summary.latest_synced_at, timezone, videoRows.length, videosQuery.data?.total])
  const rankedVideos = useMemo(
    () => filterAndSortVideos(diagnosedVideos, search, sortKey),
    [diagnosedVideos, search, sortKey],
  )
  const pageCount = Math.max(1, Math.ceil(rankedVideos.length / PAGE_SIZE))
  const currentPage = Math.min(page, pageCount)
  const pageRows = useMemo(
    () => rankedVideos.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE),
    [currentPage, rankedVideos],
  )
  const productPageCount = Math.max(1, Math.ceil(productMatrix.length / PAGE_SIZE))
  const currentProductPage = Math.min(productPage, productPageCount)
  const productPageRows = productMatrix.slice(
    (currentProductPage - 1) * PAGE_SIZE,
    currentProductPage * PAGE_SIZE,
  )
  const currency = overviewRows.find((item) => item.currency)?.currency
    || summary.videos.find((item) => item.currency)?.currency
    || 'USD'
  const loading = contextQuery.isLoading || overviewQuery.isLoading || videosQuery.isLoading
  const fetching = overviewQuery.isFetching || videosQuery.isFetching || comparisonOverviewQuery.isFetching
  const queryError = contextQuery.error || overviewQuery.error || videosQuery.error
  const mediaError = mediaQuery.error || productsQuery.error
  const operationsError = productMetricsQuery.error || guardQuery.error || comparisonOverviewQuery.error
  const detailVideo = diagnosedVideos.find((video) => video.video_id === detailVideoId) || null
  const detailMedia = detailVideo ? (mediaMap.get(String(detailVideo.video_id)) || {}) : {}
  const detailPresentation = detailVideo
    ? resolveVideoPresentation(detailVideo, detailMedia, productMap)
    : null
  const detailExecution = detailVideo ? executionByVideo.get(String(detailVideo.video_id)) : null
  const detailAnalysis = analysisQuery.data?.items?.[0] || null
  const delayed = Boolean(
    selectedRange.end_date
    && selectedRange.end_date >= todayRange.end_date
    && summary.latest_video_date !== todayRange.end_date,
  )

  useEffect(() => setPage(1), [search, sortKey, shopId, selectedRange.start_date, selectedRange.end_date])
  useEffect(() => setProductPage(1), [shopId, selectedRange.start_date, selectedRange.end_date])
  useEffect(() => {
    setPlayback(null)
    setDetailVideoId(null)
  }, [shopId])

  if (!contextQuery.isLoading && shops.length === 0) {
    return (
      <main className="shop-video-page">
        <header className="shop-video-page__header">
          <div><span className="shop-video-eyebrow">SHOP ANALYTICS</span><h1>短视频经营分析</h1></div>
        </header>
        <section className="commerce-empty commerce-empty--action">
          <strong>尚未连接 TikTok Shop</strong>
          <span>完成卖家授权后，系统会自动同步店铺短视频表现。</span>
          <Link className="btn" to={`/tenants/${wid}/tiktok-shop`}>前往授权</Link>
        </section>
      </main>
    )
  }

  return (
    <main className="shop-video-page">
      <header className="shop-video-page__header">
        <div>
          <span className="shop-video-eyebrow">CONTENT OPERATIONS</span>
          <h1>内容运营中心</h1>
          <p>从官方数据发现机会、解释原因，并追踪GMV Max执行结果。</p>
        </div>
        <div className="shop-video-page__actions">
          <Link className="btn ghost" to={`/tenants/${wid}/tiktok-shop`}>授权管理</Link>
          <button
            className="btn"
            type="button"
            disabled={fetching || !shopId}
            onClick={() => Promise.all([
              overviewQuery.refetch(),
              comparisonOverviewQuery.refetch(),
              videosQuery.refetch(),
              mediaQuery.refetch(),
              productsQuery.refetch(),
              productMetricsQuery.refetch(),
              guardQuery.refetch(),
            ])}
          >
            {fetching ? '刷新中…' : '刷新数据'}
          </button>
        </div>
      </header>

      <section className="shop-video-scope" aria-label="视频数据范围">
        <label>
          <span>分析店铺</span>
          <select value={shopId} onChange={(event) => setShopId(event.target.value)}>
            {shops.map((shop) => <option key={shop.id} value={shop.id}>{shop.name}</option>)}
          </select>
        </label>
        <div>
          <span>店铺区域</span>
          <strong>{selectedShop?.region || '--'}</strong>
          <small>{selectedShop?.provider_shop_id || ''}</small>
        </div>
        <div>
          <span>统计时区</span>
          <strong>{timezone}</strong>
          <small>日期边界以店铺时区为准</small>
        </div>
        <div>
          <span>数据新鲜度</span>
          <strong>{formatDateTime(summary.latest_synced_at)}</strong>
          <small>{trustMeta.refresh_label}</small>
        </div>
      </section>

      <nav className="shop-ops-tabs" aria-label="内容运营工作区">
        {WORKSPACE_TABS.map((tab) => (
          <button
            key={tab.value}
            type="button"
            className={activeTab === tab.value ? 'is-active' : ''}
            onClick={() => {
              setActiveTab(tab.value)
              if (tab.value === 'today') setPreset('today')
            }}
          >
            {tab.label}
            {tab.value === 'today' && diagnosisSummary.actionable.length > 0 && (
              <small>{diagnosisSummary.actionable.length}</small>
            )}
          </button>
        ))}
      </nav>

      <section className="shop-video-range">
        <div className="commerce-segments" role="group" aria-label="日期范围">
          {RANGE_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={preset === option.value ? 'is-active' : ''}
              onClick={() => setPreset(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        {preset === 'custom' && (
          <div className="commerce-date-inputs">
            <input
              type="date"
              value={customRange.start_date}
              onChange={(event) => setCustomRange((current) => ({ ...current, start_date: event.target.value }))}
            />
            <span>至</span>
            <input
              type="date"
              value={customRange.end_date}
              onChange={(event) => setCustomRange((current) => ({ ...current, end_date: event.target.value }))}
            />
          </div>
        )}
        <strong>{rangeLabel(selectedRange.start_date, selectedRange.end_date)}</strong>
      </section>

      {!validRange && <div className="alert alert--error">请选择有效的开始和结束日期。</div>}
      {queryError && <div className="alert alert--error">{errorMessage(queryError)}</div>}
      {mediaError && !queryError && (
        <div className="shop-video-delay" role="status">
          <strong>视频媒体加载失败</strong>
          <span>{errorMessage(mediaError)}；经营数据仍可正常查看。</span>
        </div>
      )}
      {operationsError && !queryError && (
        <div className="shop-video-delay" role="status">
          <strong>部分运营辅助数据暂不可用</strong>
          <span>{errorMessage(operationsError)}；视频经营数据仍可正常查看。</span>
        </div>
      )}
      {delayed && !loading && (
        <div className="shop-video-delay" role="status">
          <strong>今日只有视频聚合，逐视频明细更新至 {summary.latest_video_date || yesterdayRange.end_date}</strong>
          <span>官方逐视频列表为 T+1；今日 GMV、订单、商品曝光和点击仍按实时概览展示，不会把明细缺失误显示为全店零。</span>
        </div>
      )}

      {loading ? <Loading text="正在生成内容运营结论…" /> : (
        <>
          <section className="shop-video-day-comparison" aria-label="今日昨日视频概览">
            <div className="shop-video-panel__header">
              <div><h2>今日与昨日</h2><p>今日为可变快照，昨日为跨日核准后的稳定日表；两者不混算。</p></div>
              <TrustBadge tone="official">店铺时区 {timezone}</TrustBadge>
            </div>
            <div className="shop-video-day-comparison__grid">
              <DayOverviewCard title="今日实时" day={todayOverview} />
              <DayOverviewCard title="昨日结算" day={yesterdayOverview} stable />
            </div>
          </section>

          {activeTab === 'today' && (
            <>
              <section className="shop-ops-briefing">
                <div>
                  <span className="shop-video-eyebrow">{preset === 'today' ? '今日运营结论' : '所选周期运营结论'}</span>
                  <h2>
                    发现 {formatNumber(diagnosisSummary.actionable.length)} 项值得处理的内容机会与风险
                  </h2>
                  <p>所有结论均来自透明规则；样本不足的视频不会触发放量或暂停建议。</p>
                </div>
                <div className="shop-ops-briefing__counts">
                  <span><strong>{formatNumber(diagnosisSummary.counts.WINNER || 0)}</strong><small>爆款放大</small></span>
                  <span><strong>{formatNumber(diagnosisSummary.counts.POTENTIAL || 0)}</strong><small>潜力测试</small></span>
                  <span><strong>{formatNumber(diagnosisSummary.counts.DECAYING || 0)}</strong><small>素材衰退</small></span>
                  <span><strong>{formatNumber((diagnosisSummary.counts.TRAFFIC_NO_CLICK || 0) + (diagnosisSummary.counts.PRODUCT_HANDOFF || 0))}</strong><small>转化异常</small></span>
                </div>
              </section>

              <section className="shop-video-kpis" aria-label="短视频核心指标">
                <MetricCard label="视频 GMV" value={formatMoney(summary.gmv, currency)} detail={`${formatNumber(summary.orders)} 个 SKU 订单`} tone="money" />
                <MetricCard label="参与分析视频" value={formatNumber(diagnosedVideos.length)} detail={`${formatNumber(videoRows.length)} 条完整日明细`} />
                <MetricCard label="播放量" value={formatNumber(summary.views)} detail={`千次播放产出 ${formatMoney(summary.gpm, currency)}`} />
                <MetricCard label="商品曝光" value={formatNumber(summary.impressions)} detail={`${formatNumber(summary.clicks)} 次商品点击`} />
                <MetricCard label="官方视频点击率" value={formatPercent(summary.ctr)} detail="商品点击 ÷ 视频播放" tone="rate" />
                <MetricCard label="商品曝光点击比" value={formatPercent(summary.product_impression_click_rate)} detail="派生值：商品点击 ÷ 商品曝光" />
              </section>

              <section className="shop-video-panel">
                <div className="shop-video-panel__header">
                  <div><h2>优先行动队列</h2><p>按风险、机会和GMV影响排序，点击查看判断依据。</p></div>
                  <button className="btn sm ghost" type="button" onClick={() => setActiveTab('videos')}>查看全部内容</button>
                </div>
                <div className="shop-ops-action-list">
                  {diagnosisSummary.actionable.length ? diagnosisSummary.actionable.slice(0, 6).map((video) => (
                    <ActionCard key={video.video_id} video={video} execution={executionByVideo.get(String(video.video_id))} onOpen={(item) => setDetailVideoId(item.video_id)} />
                  )) : <div className="commerce-empty">当前没有达到规则阈值的明确机会或风险，系统会继续观察。</div>}
                </div>
              </section>

              <section className="shop-video-panel">
                <div className="shop-video-panel__header">
                  <div><h2>商品转化链路</h2><p>只展示官方具备的口径，不把播放量误接到商品曝光漏斗。</p></div>
                  <TrustBadge tone="official">官方数据</TrustBadge>
                </div>
                <div className="shop-ops-funnel">
                  <span><small>视频播放</small><strong>{formatNumber(summary.views)}</strong><em>内容触达参考</em></span>
                  <i aria-hidden="true">＋</i>
                  <span><small>商品曝光</small><strong>{formatNumber(summary.impressions)}</strong><em>官方商品口径</em></span>
                  <i aria-hidden="true">→</i>
                  <span><small>商品点击</small><strong>{formatNumber(summary.clicks)}</strong><em>{formatPercent(summary.product_impression_click_rate)} 曝光点击比</em></span>
                  <i aria-hidden="true">→</i>
                  <span><small>SKU订单</small><strong>{formatNumber(summary.orders)}</strong><em>店铺自然日</em></span>
                  <i aria-hidden="true">→</i>
                  <span><small>视频GMV</small><strong>{formatMoney(summary.gmv, currency)}</strong><em>官方汇总 · 视频点击率 {formatPercent(summary.ctr)}</em></span>
                </div>
              </section>

              <section className="shop-video-panel">
                <div className="shop-video-panel__header">
                  <div><h2>经营趋势</h2><p>左轴为视频 GMV，右轴为播放量。</p></div>
                  <span className="shop-video-panel__meta">{trend.length} 个自然日</span>
                </div>
                <VideoTrendChart rows={trend} currency={currency} />
              </section>
            </>
          )}

          {activeTab === 'videos' && (
            <section className="shop-video-panel">
              <div className="shop-video-toolbar">
                <div>
                  <h2>内容诊断雷达</h2>
                  <p>本地媒体 {formatNumber(mediaQuery.data?.matched || 0)} / {formatNumber(videoIds.length)}；点击“查看诊断”了解原因与建议。</p>
                </div>
                <div className="shop-video-toolbar__controls">
                  <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索标题、达人、视频或商品" aria-label="搜索视频" />
                  <select value={sortKey} onChange={(event) => setSortKey(event.target.value)} aria-label="视频排序方式">
                    {SORT_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                  </select>
                </div>
              </div>
              <div className="shop-video-table-wrap">
                <table className="shop-video-table shop-video-table--diagnosis">
                  <thead><tr><th>优先级</th><th>视频</th><th>播放量<small className="shop-video-column-note">Shop</small></th><th>点击估算<small className="shop-video-column-note">播放量 × 点击率</small></th><th>商品点击率<small className="shop-video-column-note">Shop</small></th><th>视频 GMV<small className="shop-video-column-note">Shop</small></th><th>订单<small className="shop-video-column-note">Shop</small></th><th>关联商品</th><th>发布时间<small className="shop-video-column-note">店铺时区</small></th><th>系统诊断</th><th>操作</th></tr></thead>
                  <tbody>
                    {pageRows.length === 0 ? <tr><td colSpan={11}><div className="commerce-empty">所选条件暂无单视频数据。</div></td></tr> : pageRows.map((video, index) => {
                      const rank = (currentPage - 1) * PAGE_SIZE + index + 1
                      const media = mediaMap.get(String(video.video_id)) || { media_status: mediaQuery.isLoading ? 'PROCESSING' : 'NOT_IN_GMVMAX_LIBRARY' }
                      const presentation = resolveVideoPresentation(video, media, productMap)
                      return (
                        <tr key={video.video_id}>
                          <td><span className={`shop-video-rank ${rank <= 3 ? 'is-top' : ''}`}>{rank}</span></td>
                          <td><div className="shop-video-title-cell"><VideoThumbnail video={video} presentation={presentation} onPlay={() => setPlayback({ video, presentation })} /><div><strong title={video.title}>{shortText(video.title, 38)}</strong><small>@{video.creator_username || '未知'} · {creatorType(video.author_type)} · {formatDuration(video.duration_seconds)}</small><span className={`shop-video-media-status is-${presentation.status_tone}`}>{presentation.status_label}</span></div></div></td>
                          <td className="shop-video-metric-cell"><strong>{formatNumber(video.views)}</strong><small>官方播放</small></td>
                          <td className="shop-video-metric-cell"><strong>≈ {formatNumber(video.estimated_product_clicks)}</strong><small>非官方计数字段</small></td>
                          <td className="shop-video-metric-cell"><strong>{formatPercent(video.click_through_rate)}</strong><small>官方比率</small></td>
                          <td className="shop-video-money">{formatMoney(video.gmv, video.currency || currency)}</td>
                          <td>{formatNumber(video.sku_orders)}</td>
                          <td><ProductThumbnailList products={video.products} productMap={productMap} /></td>
                          <td className="shop-video-published-at"><strong>{formatVideoPublishedAt(video.video_post_time, timezone)}</strong><small>{timezone}</small></td>
                          <td><div className="shop-ops-diagnosis-cell"><DiagnosisBadge diagnosis={video.diagnosis} /><small>{video.diagnosis.summary}</small></div></td>
                          <td><button className="btn sm ghost" type="button" onClick={() => setDetailVideoId(video.video_id)}>查看诊断</button></td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              <footer className="shop-video-pagination"><span>共 {formatNumber(rankedVideos.length)} 个视频，第 {currentPage}/{pageCount} 页</span><div><button className="btn sm ghost" type="button" disabled={currentPage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>上一页</button><button className="btn sm ghost" type="button" disabled={currentPage >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))}>下一页</button></div></footer>
            </section>
          )}

          {activeTab === 'products' && (
            <section className="shop-video-panel">
              <div className="shop-video-panel__header"><div><h2>商品内容矩阵</h2><p>商品GMV来自商品级官方报表；视频只做关联统计，不做虚假GMV分摊。</p></div><TrustBadge tone="official">精确商品口径</TrustBadge></div>
              <div className="shop-ops-product-grid">
                {productPageRows.length ? productPageRows.map((product) => (
                  <article key={product.product_id} className="shop-ops-product-card">
                    <div className="shop-ops-product-card__head">{product.image_url ? <img src={product.image_url} alt="" loading="lazy" /> : <span aria-hidden="true">商</span>}<div><strong>{shortText(product.title, 46)}</strong><small>ID {product.product_id}</small></div><TrustBadge tone={product.content_tone}>{product.content_status}</TrustBadge></div>
                    <div className="shop-ops-product-card__metrics"><span><small>商品GMV</small><strong>{formatMoney(product.gmv, product.currency)}</strong></span><span><small>订单</small><strong>{formatNumber(product.orders)}</strong></span><span><small>关联视频</small><strong>{formatNumber(product.linked_video_count)}</strong></span><span><small>有效视频</small><strong>{formatNumber(product.effective_video_count)}</strong></span></div>
                    <footer><span>点击率 {formatPercent(product.click_through_rate)}</span><span>退款额占GMV {formatPercent(product.refund_rate)}</span><span>{product.opportunity_count ? `${product.opportunity_count} 条放量机会` : '暂无明确放量机会'}</span></footer>
                  </article>
                )) : <div className="commerce-empty">所选日期暂无商品分析数据。</div>}
              </div>
              <footer className="shop-video-pagination"><span>共 {formatNumber(productMatrix.length)} 个商品，第 {currentProductPage}/{productPageCount} 页</span><div><button className="btn sm ghost" type="button" disabled={currentProductPage <= 1} onClick={() => setProductPage((value) => Math.max(1, value - 1))}>上一页</button><button className="btn sm ghost" type="button" disabled={currentProductPage >= productPageCount} onClick={() => setProductPage((value) => Math.min(productPageCount, value + 1))}>下一页</button></div></footer>
            </section>
          )}

          {activeTab === 'guard' && (
            <>
              <section className="shop-video-panel">
                <div className="shop-video-panel__header"><div><h2>GMV Max实时守护状态</h2><p>Smart Guard每分钟复核；这里展示最近一次检查与官方投放状态。</p></div><TrustBadge tone="calculated">60秒刷新</TrustBadge></div>
                <div className="shop-ops-guard-states">
                  {(guardQuery.data?.states || []).slice(0, 12).map((state) => (
                    <article key={state.id}><header><strong>{state.campaign_name || `广告系列 ${state.campaign_id}`}</strong><TrustBadge tone={String(state.operation_status).toUpperCase().includes('ENABLE') ? 'success' : 'warning'}>{state.operation_status || '状态未知'}</TrustBadge></header><p>{state.last_reason || '当前没有需要展示的额外原因。'}</p><div><span>ROI <strong>{formatRatio(state.roi)}</strong></span><span>订单 <strong>{formatNumber(state.orders)}</strong></span><span>最近检查 <strong>{formatDateTime(state.last_checked_at)}</strong></span></div><Link to={`/tenants/${wid}/gmvmax`}>进入GMV Max管理 →</Link></article>
                  ))}
                  {!guardQuery.isLoading && !(guardQuery.data?.states || []).length && <div className="commerce-empty">当前店铺尚无GMV Max实时守护状态。</div>}
                </div>
              </section>
              <section className="shop-video-panel"><div className="shop-video-panel__header"><div><h2>执行与官方返回时间线</h2><p>展示触发原因、执行结果、官方请求标识和失败信息。</p></div><span className="shop-video-panel__meta">最近 {formatNumber((guardQuery.data?.items || []).length)} 条</span></div><GuardTimeline items={guardQuery.data?.items} workspaceId={wid} /></section>
            </>
          )}

          {activeTab === 'review' && (
            <>
              <section className="shop-ops-review-grid">
                <article><small>规则发现</small><strong>{formatNumber(diagnosisSummary.actionable.length)}</strong><p>当前范围内达到明确阈值的机会和风险。</p></article>
                <article><small>系统执行记录</small><strong>{formatNumber((guardQuery.data?.items || []).length)}</strong><p>最近Smart Guard与素材守护动作。</p></article>
                <article><small>执行成功</small><strong>{formatNumber((guardQuery.data?.items || []).filter((item) => String(item.result).toUpperCase().includes('SUCCESS')).length)}</strong><p>已记录为成功的官方动作。</p></article>
                <article><small>执行异常</small><strong>{formatNumber((guardQuery.data?.items || []).filter((item) => item.error_message || String(item.result).toUpperCase().includes('FAIL')).length)}</strong><p>需要进入GMV Max进一步处理。</p></article>
              </section>
              <section className="shop-video-panel"><div className="shop-video-panel__header"><div><h2>策略结果复盘</h2><p>仅当守护记录明确带有相同创意ID时关联执行结果，不做未经证实的一对一归因。</p></div><TrustBadge tone="advice">可解释复盘</TrustBadge></div><div className="shop-ops-review-list">{diagnosisSummary.actionable.slice(0, 12).map((video) => <ActionCard key={video.video_id} video={video} execution={executionByVideo.get(String(video.video_id))} onOpen={(item) => setDetailVideoId(item.video_id)} />)}{!diagnosisSummary.actionable.length && <div className="commerce-empty">当前范围暂无需要复盘的明确建议。</div>}</div></section>
            </>
          )}

          <section className="shop-video-notes">
            <div><strong>数据来源</strong><span>{trustMeta.source} · {trustMeta.grain}</span></div>
            <div><strong>新鲜度</strong><span>{trustMeta.freshness_label} · {trustMeta.refresh_label}</span></div>
            <div><strong>最近同步</strong><span>{formatDateTime(summary.latest_synced_at)} · 单视频至 {summary.latest_video_date || '暂无'}</span></div>
            <div><strong>加载完整性</strong><span>{trustMeta.completeness_label} · {formatNumber(videoRows.length)} / {formatNumber(videosQuery.data?.total || 0)} 条</span></div>
          </section>
        </>
      )}
      <VideoPlayerModal playback={playback} onClose={() => setPlayback(null)} />
      <VideoDiagnosisDrawer
        video={detailVideo}
        presentation={detailPresentation}
        execution={detailExecution}
        analysis={detailAnalysis}
        analysisError={analysisQuery.error || analysisMutation.error}
        analyzing={analysisMutation.isPending || analysisQuery.isFetching}
        workspaceId={wid}
        onAnalyze={() => analysisMutation.mutate()}
        onClose={() => setDetailVideoId(null)}
        onPlay={() => detailVideo && setPlayback({ video: detailVideo, presentation: detailPresentation })}
      />
    </main>
  )
}
