import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { useSessionQuery } from '@/features/platform/auth/hooks.js'
import { getProviderStatus as getAiVideoProviderStatus } from '@/features/tenants/ai_video/service.js'
import {
  addContentFactoryProducerReferenceLink,
  bindContentFactoryBridgeDevice,
  contentFactoryAssetUrl,
  contentFactoryDeliverablesZipUrl,
  contentFactoryProductAssetUrl,
  confirmContentFactoryProducerProject,
  createContentFactoryProduct,
  deleteContentFactoryProducerAttachment,
  deleteContentFactoryProduct,
  deleteContentFactoryProductAsset,
  deleteContentFactoryProject,
  downloadContentFactoryBridgeAgent,
  fetchContentFactoryBridge,
  fetchContentFactoryProducts,
  fetchContentFactoryProducerSession,
  fetchContentFactoryProjects,
  generateContentFactoryProductFacts,
  pauseContentFactoryProject,
  prepareContentFactoryBridgeSlot,
  removeContentFactoryBridgeSlot,
  restartContentFactoryProject,
  resumeContentFactoryProject,
  runContentFactoryStage,
  selectContentFactoryBridgeDevice,
  sendContentFactoryProducerTurn,
  updateContentFactoryProduct,
  updateContentFactoryProject,
  unbindContentFactoryBridgeDevice,
  uploadContentFactoryAssets,
  uploadContentFactoryProducerAttachments,
  uploadContentFactoryProductAssets,
} from '../api.js'
import './ContentFactoryPage.css'

const STAGES = ['FACTS', 'SERIES_DIRECTOR', 'DIRECTOR', 'PRODUCTION_PLAN', 'VISUAL_PREVIEW', 'CREATIVE_REVIEW', 'FINAL_ASSETS', 'VIDEO_PROMPTS', 'WAITING_VIDEO_INPUT', 'EDIT_PACKAGE', 'COMPLETE']
const EXECUTION_STAGES = ['FACTS', 'SERIES_DIRECTOR', 'DIRECTOR', 'PRODUCTION_PLAN', 'VISUAL_PREVIEW', 'CREATIVE_REVIEW', 'FINAL_ASSETS', 'VIDEO_PROMPTS', 'EDIT_PACKAGE']
const RESTART_STAGES = ['SERIES_DIRECTOR', 'DIRECTOR', 'PRODUCTION_PLAN', 'VISUAL_PREVIEW', 'CREATIVE_REVIEW', 'FINAL_ASSETS', 'VIDEO_PROMPTS', 'EDIT_PACKAGE']
const PRODUCER_RECOVERY_DEADLINE_MS = 210_000
const PRODUCER_RECOVERY_INTERVAL_MS = 2_000
const PRODUCER_SUPPORTING_MATERIAL_ACCEPT = [
  '.docx', '.docm', '.dotx', '.dotm',
  '.xlsx', '.xlsm', '.xltx', '.xltm',
  '.pptx', '.pptm', '.potx', '.potm', '.ppsx', '.ppsm',
  '.odt', '.ods', '.odp', '.pdf',
  '.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.jsonl',
  '.yaml', '.yml', '.xml', '.html', '.htm', '.tex', '.log',
  '.c', '.cc', '.cpp', '.h', '.hpp', '.cs', '.css', '.go', '.java',
  '.js', '.jsx', '.php', '.py', '.rb', '.sh', '.sql', '.ts', '.tsx',
  'image/jpeg', 'image/png', 'image/webp', '.jpg', '.jpeg', '.png', '.webp',
].join(',')
const LABELS = {
  FACTS: '产品事实',
  SERIES_DIRECTOR: '整批编导',
  DIRECTOR: '单条编导',
  PRODUCTION_PLAN: '制作方案',
  VISUAL_PREVIEW: '视觉预演',
  CREATIVE_REVIEW: '视觉验收',
  FINAL_ASSETS: '正式参考图',
  VIDEO_PROMPTS: '分段执行编译',
  WAITING_VIDEO_INPUT: '生成视频',
  EDIT_PACKAGE: '投放发布包',
  COMPLETE: '已完成',
}

LABELS.EDIT_PACKAGE = '\u526a\u8f91\u53d1\u5e03\u6307\u5bfc'
LABELS.BENCHMARK_VIDEO = '\u5bf9\u6807\u89c6\u9891\u62c6\u89e3'
LABELS.CHARACTER_REFERENCE = '\u4eba\u7269\u53c2\u8003\u56fe'

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

function isProducerConfirmationMessage(value) {
  return /^(?:确认|确认执行|确认创建|开始吧|开始执行|就按这个方案|按这个方案执行)[。！!\s]*$/u.test(String(value || '').trim())
}

function isProducerExecutionMessage(value) {
  const message = String(value || '').trim()
  if (!message || /(?:不要|不用|先别|暂不|停止|取消)(?:再)?(?:生成|制作|创建|执行|开始)/u.test(message)) return false
  return /(?:生成|制作|创建|开始做|开始生成|开始制作|执行)(?:[^。！？!?\n]{0,40})(?:视频|任务|项目|版本|条|支)/u.test(message)
}

function producerDeliveryModeLabel(value) {
  return {
    single: '单条成片',
    independent_videos: '多条独立视频',
    series_episodes: '系列化独立视频',
    visual_variants: '同一文案的视觉变体',
  }[value] || '按当前需求执行'
}

async function recoverProducerTurn(wid, sessionKey, baselineMessageCount, startedAt) {
  const deadline = startedAt + PRODUCER_RECOVERY_DEADLINE_MS
  let latestSession = null
  while (Date.now() < deadline) {
    try {
      latestSession = await fetchContentFactoryProducerSession(wid, sessionKey)
      const messages = Array.isArray(latestSession?.messages) ? latestSession.messages : []
      const lastMessage = messages[messages.length - 1]
      // One successful turn durably appends exactly one user message followed
      // by one assistant message.  Never resend a timed-out turn: wait for the
      // already-running backend request to commit that assistant reply.
      if (messages.length >= baselineMessageCount + 2 && lastMessage?.role === 'assistant') {
        return latestSession
      }
    } catch (_) {
      // A transient network failure can affect the recovery GET as well. Keep
      // the bounded poll alive without creating another model request.
    }
    const remaining = deadline - Date.now()
    if (remaining > 0) await delay(Math.min(PRODUCER_RECOVERY_INTERVAL_MS, remaining))
  }
  return null
}

function normalizeVideoModel(model) {
  if (['seedance_2_0', 'seedence_2_0', 'seedence_2_0_mini'].includes(model)) return 'seedance_2_0_mini'
  if (['omni_flash', 'gemini_omni_flash', 'google_omni_flash'].includes(model)) return 'omni_flash'
  return ''
}

function videoModelRecord(models, model) {
  const normalized = normalizeVideoModel(model)
  return (Array.isArray(models) ? models : []).find((item) => item?.id === normalized) || null
}

function videoReferenceLimit(models, model) {
  return Math.max(1, Number(videoModelRecord(models, model)?.reference_image_limit || 1))
}

function videoSegmentLabel(models, model) {
  const record = videoModelRecord(models, model)
  if (!record) return '未选择视频模型'
  const durations = Array.isArray(record.available_durations_seconds) ? record.available_durations_seconds : []
  return durations.length
    ? `${record.label} 单片支持 ${durations.join('/')} 秒`
    : `${record.label} 按当前供应商能力规划时长`
}

function statusTone(status) {
  if (status === 'success' || status === 'complete') return { background: '#e9f8ef', color: '#167a3f' }
  if (status === 'failed') return { background: '#fff0f0', color: '#c93636' }
  if (status === 'superseded') return { background: '#f3f5f8', color: '#586174' }
  if (status === 'paused') return { background: '#fff7e6', color: '#9a5b00' }
  if (status === 'running' || status === 'queued' || status === 'generating' || status === 'generating_video') return { background: '#eef5ff', color: '#2469c8' }
  if (status === 'needs_update') return { background: '#fff7e6', color: '#9a5b00' }
  return { background: '#f3f5f8', color: '#586174' }
}

function stageDisplayStatus(stage) {
  const message = String(stage?.error_message || '')
  if (stage?.status === 'failed' && message.startsWith('Superseded by ')) return 'superseded'
  return stage?.status || ''
}

function stageStatusText(stage) {
  const status = stageDisplayStatus(stage)
  if (status === 'superseded') return '已跳过'
  return status
}

function projectStatusText(project) {
  const stage = LABELS[project?.current_stage] || project?.current_stage || '准备中'
  if (project?.status === 'complete') return '全部视频已完成'
  if (project?.status === 'paused') return `已暂停 · ${stage}`
  if (project?.status === 'failed') return `需要处理 · ${stage}`
  if (project?.status === 'generating_video') return '视频生成、下载与合成中'
  if (project?.status === 'running') return `正在执行 · ${stage}`
  if (project?.status === 'queued') return `已排队 · ${stage}`
  return stage
}

function recoveryActionText(action) {
  const labels = {
    WAIT_AND_RETRY_API: '等待后重试 API',
    SWITCH_TO_API: '切回 API',
    SWITCH_TO_BROWSER: '切换浏览器兜底',
    RETRY_BROWSER: '重试浏览器',
    WAIT_FOR_BROWSER: '等待浏览器恢复',
    ROTATE_PROVIDER: '轮换供应商或账号',
    SEMANTIC_PROMPT_REPAIR: '多模态修复提示词',
    RECOMPILE_STAGE_INPUT: '按用户意图重编译本阶段',
    RECONCILE_LATE_RESULT: '核对已提交任务结果',
    PAUSE_NONRETRYABLE: '等待外部授权',
  }
  return labels[action] || action || '分析中'
}

function deliverableStatusLabel(status) {
  if (status === 'complete') return '已交付'
  if (status === 'waiting_guidance') return '待指导'
  if (status === 'guide_only') return '缺视频'
  return '待生成'
}

function deliverableStatusTone(status) {
  if (status === 'complete') return statusTone('success')
  if (status === 'waiting_guidance') return statusTone('running')
  if (status === 'guide_only') return statusTone('failed')
  return statusTone('queued')
}

function formatFileSize(value) {
  const size = Number(value || 0)
  if (!size) return ''
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

function slotTone(slot) {
  if (!slot?.connected && slot?.agent_last_heartbeat_at) return { background: '#fff7e6', color: '#9a5b00', borderColor: '#f0c36d' }
  if (!slot?.connected) return { background: '#fff0f0', color: '#c93636', borderColor: '#ffd2d2' }
  if (slot?.usage_status === 'occupied') return { background: '#eef5ff', color: '#2469c8', borderColor: '#bdd7ff' }
  if (slot?.auth_status === 'login_required') return { background: '#fff7e6', color: '#9a5b00', borderColor: '#f0c36d' }
  if (slot?.auth_status !== 'ready') return { background: '#f5f6f8', color: '#5b6472', borderColor: '#d9dde5' }
  return { background: '#e9f8ef', color: '#167a3f', borderColor: '#bde8cc' }
}

function bridgeHasLiveAgent(bridge) {
  return Array.isArray(bridge?.slots) && bridge.slots.some((slot) => Boolean(slot?.agent_last_heartbeat_at))
}

function bridgeTone(bridge) {
  if (bridge?.connected) return statusTone('success')
  if (bridgeHasLiveAgent(bridge)) return { background: '#fff7e6', color: '#9a5b00' }
  return statusTone('failed')
}

function bridgeStatusText(bridge) {
  if (bridge?.connected) return `当前设备浏览器已连接 · ${bridge.browser || 'Chrome'}`
  if (bridge?.detail) return bridge.detail
  if (bridgeHasLiveAgent(bridge)) return '浏览器桥程序已在线，但 Chrome/CDP 隧道未连通'
  return '当前设备浏览器桥未连接'
}

function slotStatusText(slot, activeProject) {
  if (slot?.connected && slot?.auth_status === 'login_required') return '待登录 ChatGPT'
  if (slot?.connected && slot?.auth_status !== 'ready') return '检测登录状态'
  if (slot?.connected) return activeProject ? '占用中' : '登录可用'
  if (slot?.agent_last_heartbeat_at) return '程序在线 · CDP未通'
  return '未连接'
}

function getBridgeDeviceId(wid) {
  const key = `content-factory-bridge-device:${wid}`
  let value = ''
  try { value = window.localStorage.getItem(key) || '' } catch (_) { value = '' }
  if (!value) {
    value = `dev_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`
    try { window.localStorage.setItem(key, value) } catch (_) { /* ignore */ }
  }
  return value
}

function factsLabel(product) {
  const status = product?.meta_json?.facts_status
  if (status === 'queued' || status === 'running') return '产品事实生成中'
  if (status === 'failed') return '产品事实失败'
  if (product?.facts_json) return '产品事实已完成'
  if (status === 'needs_update') return '资料已更新，待生成事实'
  return '待生成产品事实'
}

const PRODUCER_QUICK_STARTS = [
  {
    label: '带货转化',
    prompt: '我要做 TikTok 带货转化视频。请结合所选商品、目标受众和购买理由，推荐视频数量、时长、风格、节奏与 CTA；未经我确认的价格和促销不要自行补充。',
  },
  {
    label: '故事科普',
    prompt: '我要做有故事、有知识点的 TikTok 科普短视频。请先理解受众痛点，再推荐合适的叙事方式、时长、数量、画面风格和声音方案。',
  },
  {
    label: '动画短片',
    prompt: '我要做适合 TikTok 的动画短片，开头三秒必须抓人，节奏紧凑。请根据目标推荐动画风格、时长、数量、声音和转化结构。',
  },
  {
    label: '已有完整文案',
    prompt: '我已经有完整文案。请保留原文核心含义和转化逻辑，判断适合的时长与分段方式，并推荐画面风格、节奏和声音身份。',
  },
]

function newCharacterDraft(index = 1) {
  return {
    key: `character_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
    name: `人物 ${index}`,
    description: '',
    files: [],
  }
}

function newProducerSessionKey() {
  const random = globalThis.crypto?.randomUUID?.().replaceAll('-', '') || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`
  return `intake-${random.slice(0, 24)}`
}

function newProducerTurnId() {
  return (globalThis.crypto?.randomUUID?.().replaceAll('-', '') || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`).slice(0, 32)
}

function producerRequestMayStillBeRunning(error) {
  const hasServerResponse = Boolean(
    error?.status
    || error?.response
    || error?.payload?.error?.code,
  )
  if (hasServerResponse) return false
  return ['ECONNABORTED', 'ERR_NETWORK', 'ETIMEDOUT'].includes(String(error?.code || ''))
    || /network|timeout|timed out/i.test(String(error?.message || ''))
}

function producerErrorMessage(error) {
  const code = String(error?.payload?.error?.code || '')
  if (code === 'HERMES_UPSTREAM_QUOTA') {
    return '内容模型供应商当前额度或线路不可用，系统已尝试所有备用线路。你的文字和附件都已保存，请稍后点击重试。'
  }
  if (code === 'HERMES_PROMPT_POLICY_VIOLATION') {
    return '提示词被上游模型明确判定违反内容政策。你的文字和附件都已保存，请调整相关表述后重试。'
  }
  if (code === 'HERMES_UPSTREAM_AUTH' || code === 'HERMES_UPSTREAM_EXECUTION_FAILED') {
    return '内容模型供应商当前不可用，系统已尝试备用线路。你的文字和附件都已保存，请稍后点击重试。'
  }
  if (code === 'CONTENT_PRODUCER_ATTACHMENTS_PROCESSING') {
    return '对标视频仍在下载、提取口播并进行多模态拆解，请等待完成后再继续。'
  }
  if (code === 'CONTENT_PRODUCER_REFERENCE_URL_INVALID') {
    return '这个链接无法安全下载。请发送 TikTok、抖音、快手、YouTube 或 Facebook 的公开 HTTPS 视频链接。'
  }
  if (code === 'CONTENT_PRODUCER_BENCHMARK_ANALYSIS_FAILED') {
    return '对标视频没有完成多模态拆解。请移除后重试链接，或上传本地视频文件。'
  }
  if (code === 'CONTENT_PRODUCER_RESPONSE_INVALID' || code === 'CONTENT_PRODUCER_REVIEWED_DECISION_INVALID') {
    return '制片助理这次没有形成完整方案，已停止等待并保留你的原话。请点击重试，系统不会重复写入消息。'
  }
  if (code === 'HERMES_TIMEOUT' || error?.status === 504) {
    return '内容模型本次响应超时。你的文字和附件都已保存，可以安全重试，不会重复写入消息。'
  }
  if (error?.status >= 500) {
    return 'AI 制片助理服务暂时不可用。你的文字和附件都已保存，请稍后重试。'
  }
  return error?.message || 'AI 制片助理暂时无法响应。'
}

const BENCHMARK_VIDEO_HOST_SUFFIXES = [
  'tiktok.com', 'douyin.com', 'iesdouyin.com', 'amemv.com', 'snssdk.com',
  'youtube.com', 'youtu.be', 'kuaishou.com', 'gifshow.com', 'kwai.com',
  'facebook.com', 'fb.watch',
]

function benchmarkVideoUrlFromText(value) {
  const candidates = String(value || '').match(/https:\/\/[^\s<>"']+/giu) || []
  for (const candidate of candidates) {
    const cleaned = candidate.replace(/[)，。！？、；：,.!?;:]+$/u, '')
    try {
      const url = new URL(cleaned)
      const host = url.hostname.toLowerCase().replace(/\.$/u, '')
      if (BENCHMARK_VIDEO_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`))) {
        return url.href
      }
    } catch (_) {
      // Keep ordinary text in the Producer turn; only route valid public URLs.
    }
  }
  return ''
}

function attachmentKindLabel(kind) {
  return {
    reference_video: '对标视频',
    character_reference: '人物参考图',
    brief_document: '项目文档',
    creative_reference: '创意参考图',
  }[kind] || '附件'
}

function formatAttachmentSize(bytes) {
  const value = Number(bytes || 0)
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`
  return `${Math.max(1, Math.round(value / 1024))} KB`
}

function attachmentAnalysisLabel(attachment) {
  if (attachment?.active === false) return '历史对标 · 本轮不使用'
  const downloadStatus = String(attachment?.analysis?.download_status || '')
  const multimodalStatus = String(attachment?.analysis?.multimodal_status || '')
  const producerTurnStatus = String(attachment?.analysis?.producer_turn_status || '')
  if (downloadStatus === 'queued' || downloadStatus === 'processing') return '正在下载公开原视频'
  if (multimodalStatus === 'queued') return '已接收，等待多模态拆解调度'
  if (['queued', 'processing'].includes(attachment?.analysis_status) || multimodalStatus === 'processing') return '正在转写并做多模态拆解'
  if (attachment?.analysis_status === 'failed' || multimodalStatus === 'failed') return '多模态拆解失败'
  if (['waiting_analysis', 'queued', 'processing'].includes(producerTurnStatus)) return '拆解完成 · 制片助理正在整理'
  if (producerTurnStatus === 'failed') return '拆解完成 · 制片回复失败'
  if (producerTurnStatus === 'success') return '拆解完成 · 已交给制片助理'
  const transcriptStatus = String(attachment?.analysis?.transcript_status || '')
  if (transcriptStatus === 'failed') return '画面完成 · 口播提取失败'
  if (transcriptStatus === 'no_speech') return '分析完成 · 无可识别口播'
  if (attachment?.kind === 'brief_document') {
    const characters = Number(attachment?.analysis?.extracted_characters || 0)
    return characters ? `正文已提取 · ${characters.toLocaleString()} 字符` : '正文已提取'
  }
  if (attachment?.kind === 'creative_reference') return '图片已识别'
  if (attachment?.kind === 'reference_video' && multimodalStatus === 'success') return '画面、口播与结构拆解完成'
  return '分析完成'
}

function contentFactoryUserStorageKey(kind, wid, userId) {
  if (!wid || !userId) return ''
  return `content-factory-${kind}:${wid}:user:${userId}`
}

function characterGroupsFromAssets(assets = []) {
  const groups = new Map()
  assets.filter((asset) => asset.kind === 'character_reference').forEach((asset) => {
    const meta = asset.meta_json || {}
    const key = meta.character_key || 'character_1'
    const group = groups.get(key) || {
      key,
      name: meta.character_name || '人物',
      description: meta.character_description || '',
      files: [],
    }
    group.files.push(asset)
    groups.set(key, group)
  })
  return Array.from(groups.values())
}

function projectSettings(project) {
  const config = project?.config_json || {}
  return {
    title: project?.title || '',
    content_objective: config.content_objective || project?.title || '',
    target_audience: config.target_audience || '',
    content_mode: config.content_mode
      ? (config.content_mode === 'product' ? 'product' : 'general')
      : (project?.product_id || project?.product_name ? 'product' : 'general'),
    product_use_mode: config.product_use_mode || (config.product_required === false ? 'none' : 'required'),
    product_brief: project?.product_brief || '',
    video_count: Number(config.video_count || 10),
    max_api_video_variants_in_flight: Number(config.max_api_video_variants_in_flight || 1),
    video_duration_min_seconds: Number(config.video_duration_min_seconds || 10),
    video_duration_max_seconds: Number(config.video_duration_max_seconds || 10),
    video_model: normalizeVideoModel(config.video_model),
    video_duration_strategy: config.video_duration_strategy || 'creative_flexibility',
    video_resolution: config.video_resolution || '720p',
    video_aspect_ratio: config.video_aspect_ratio || config.director_series_brief?.aspect_ratio || '9:16',
    video_language: config.video_language || 'en-US',
    video_reference_limit: Number(config.video_reference_limit || 7),
    video_frame_mode: config.video_frame_mode || 'reference',
    allow_reference_video: Boolean(config.allow_reference_video),
    video_generation_mode: ['text_to_video', 'image_to_video', 'video_to_video'].includes(config.video_generation_mode)
      ? config.video_generation_mode
      : 'image_to_video',
    confirmed_claims: config.confirmed_claims || '',
    confirmed_selling_points: config.confirmed_selling_points || '',
    confirmed_promotions: config.confirmed_promotions || '',
    promotion_cta: config.promotion_cta || '',
    allow_promotional_cta: config.allow_promotional_cta !== false,
    content_director_mode: 'enforce',
    auto_run: config.auto_run !== false,
  }
}

const MEDIA_CONTRACT_STAGES = new Set([
  'VISUAL_PREVIEW', 'CREATIVE_REVIEW', 'FINAL_ASSETS', 'VIDEO_PROMPTS',
  'EDIT_PACKAGE', 'COMPLETE',
])

function projectMediaContractLocked(project) {
  return Boolean(
    (project?.stages || []).some((stage) => MEDIA_CONTRACT_STAGES.has(stage?.stage))
    || (project?.assets || []).some((asset) => MEDIA_CONTRACT_STAGES.has(asset?.stage)),
  )
}

export default function ContentFactoryPage() {
  const { wid } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const handoffSessionKey = useMemo(() => {
    const value = String(searchParams.get('producer_session') || '').trim()
    return /^[A-Za-z0-9_.-]{1,48}$/.test(value) ? value : ''
  }, [searchParams])
  const sessionQuery = useSessionQuery()
  const currentUserId = sessionQuery.data?.id
  const producerStorageKey = useMemo(
    () => contentFactoryUserStorageKey('producer', wid, currentUserId),
    [wid, currentUserId],
  )
  const [tab, setTab] = useState('factory')
  const [projects, setProjects] = useState([])
  const [products, setProducts] = useState([])
  const [selectedKey, setSelectedKey] = useState('')
  const [workspaceMode, setWorkspaceMode] = useState('conversation')
  const [selectedProductId, setSelectedProductId] = useState('')
  const [bridge, setBridge] = useState({ connected: false })
  const [bridgeForm, setBridgeForm] = useState({ cdp_url: '', inbox_root: '', device_name: '' })
  const [bridgeBusy, setBridgeBusy] = useState(false)
  const [bridgeMessage, setBridgeMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [instruction, setInstruction] = useState('')
  const [targetStage, setTargetStage] = useState('')
  const emptyProductForm = { brand_name: '', product_name: '', market: 'US', product_brief: '' }
  const [productForm, setProductForm] = useState(emptyProductForm)
  const [productEditForm, setProductEditForm] = useState(emptyProductForm)
  const [videoModels, setVideoModels] = useState([])
  const [videoModelsError, setVideoModelsError] = useState('')
  const [characterUploadOpen, setCharacterUploadOpen] = useState(false)
  const [projectCharacterDraft, setProjectCharacterDraft] = useState(() => newCharacterDraft(1))
  const [projectEditForm, setProjectEditForm] = useState(null)
  const [restartStage, setRestartStage] = useState('DIRECTOR')
  const [projectEditOpen, setProjectEditOpen] = useState(false)
  const [producerSessionKey, setProducerSessionKey] = useState('')
  const [producerProductId, setProducerProductId] = useState('')
  const [producerInput, setProducerInput] = useState('')
  const [producerMessages, setProducerMessages] = useState([])
  const [producerPersistedMessageCount, setProducerPersistedMessageCount] = useState(0)
  const [producerAttachments, setProducerAttachments] = useState([])
  const [producerUploadBusy, setProducerUploadBusy] = useState(false)
  const [producerRetryTurnId, setProducerRetryTurnId] = useState('')
  const [producerRetryMessage, setProducerRetryMessage] = useState('')
  const [producerProposal, setProducerProposal] = useState(null)
  const [producerProposalSha, setProducerProposalSha] = useState('')
  const [producerStatus, setProducerStatus] = useState('idle')
  const [producerAuthoritativeScriptId, setProducerAuthoritativeScriptId] = useState(null)
  const [producerAuthoritativeScript, setProducerAuthoritativeScript] = useState('')
  const [producerAuthoritativeScriptVersion, setProducerAuthoritativeScriptVersion] = useState(null)
  const [producerIntentSpec, setProducerIntentSpec] = useState(null)
  const [producerPendingDecisionId, setProducerPendingDecisionId] = useState('')
  const [producerBusy, setProducerBusy] = useState(false)
  const [producerProgress, setProducerProgress] = useState('')
  const [producerSessionLoadError, setProducerSessionLoadError] = useState('')
  const [producerSessionReloadNonce, setProducerSessionReloadNonce] = useState(0)
  const producerPanelRef = useRef(null)
  const producerMessagesRef = useRef(null)
  const producerComposerRef = useRef(null)
  const [producerFocusNonce, setProducerFocusNonce] = useState(0)
  const [producerBriefOpen, setProducerBriefOpen] = useState(false)
  const producerAttachmentAnalysisPending = producerAttachments.some(
    (item) => item?.active !== false && (
      ['queued', 'processing'].includes(item.analysis_status)
      || ['waiting_analysis', 'queued', 'processing'].includes(item?.analysis?.producer_turn_status)
    ),
  )

  const selected = useMemo(() => projects.find((item) => item.project_key === selectedKey) || projects[0] || null, [projects, selectedKey])
  const selectedProduct = useMemo(() => products.find((item) => String(item.id) === String(selectedProductId)) || null, [products, selectedProductId])
  const selectedCharacterGroups = useMemo(() => characterGroupsFromAssets(selected?.assets || []), [selected?.assets])
  const mediaContractLocked = useMemo(() => projectMediaContractLocked(selected), [selected])
  const selectedBridgeDeviceId = String(bridge?.selected_device_id || '')
  const currentDeviceSlots = useMemo(
    () => (Array.isArray(bridge?.slots) ? bridge.slots : []).filter(
      (slot) => String(slot?.agent_device_id || '') === selectedBridgeDeviceId,
    ),
    [bridge?.slots, selectedBridgeDeviceId],
  )

  const focusProducerComposer = useCallback(() => {
    setWorkspaceMode('conversation')
    setProducerFocusNonce((value) => value + 1)
  }, [])

  useEffect(() => {
    if (!producerFocusNonce) return undefined
    const timer = window.setTimeout(() => {
      producerPanelRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
      producerComposerRef.current?.focus?.({ preventScroll: true })
    }, 80)
    return () => window.clearTimeout(timer)
  }, [producerFocusNonce])

  useEffect(() => {
    const node = producerMessagesRef.current
    if (!node) return
    node.scrollTop = node.scrollHeight
  }, [producerMessages, producerBusy, producerProgress])

  useEffect(() => {
    if (producerStatus === 'proposal_ready') setProducerBriefOpen(true)
  }, [producerStatus])

  useEffect(() => {
    setBridgeForm((prev) => ({
      ...prev,
      device_name: prev.device_name || `${navigator.platform || 'Browser'} · ${navigator.userAgent.includes('Chrome') ? 'Chrome' : 'Browser'}`,
      inbox_root: prev.inbox_root || 'C:\\Users\\sqkj01\\AppData\\Local\\MYUPONA\\HermesInbox',
    }))
  }, [wid])

  useEffect(() => {
    if (!producerStorageKey) return undefined
    let cancelled = false
    let savedKey = handoffSessionKey
    if (!savedKey) {
      try { savedKey = window.localStorage.getItem(producerStorageKey) || '' } catch (_) { savedKey = '' }
    } else {
      try { window.localStorage.setItem(producerStorageKey, savedKey) } catch (_) { /* ignore */ }
    }
    if (!savedKey) {
      setProducerSessionLoadError('')
      setProducerSessionKey(newProducerSessionKey())
      return () => { cancelled = true }
    }
    setProducerSessionKey(savedKey)
    fetchContentFactoryProducerSession(wid, savedKey).then((session) => {
      if (cancelled) return
      setProducerSessionLoadError('')
      const messages = Array.isArray(session?.messages) ? session.messages : []
      setProducerMessages(messages)
      setProducerPersistedMessageCount(messages.length)
      setProducerAttachments(Array.isArray(session?.attachments) ? session.attachments : [])
      const lastMessage = messages[messages.length - 1]
      if (lastMessage?.role === 'user' && lastMessage?.client_turn_id) {
        setProducerInput(lastMessage.content || '')
        setProducerRetryMessage(lastMessage.content || '')
        setProducerRetryTurnId(lastMessage.client_turn_id)
      } else if (!messages.length && session?.draft_message) {
        setProducerInput(session.draft_message)
      }
      setProducerProposal(session?.proposal || null)
      setProducerProposalSha(session?.proposal_sha256 || '')
      setProducerStatus(session?.status || 'idle')
      setProducerAuthoritativeScriptId(session?.authoritative_script_message_id || null)
      setProducerAuthoritativeScript(session?.authoritative_script || '')
      setProducerAuthoritativeScriptVersion(session?.authoritative_script_version || null)
      setProducerIntentSpec(session?.intent_spec || null)
      setProducerPendingDecisionId(session?.pending_decision_id || '')
      if (session?.selected_product_id) setProducerProductId(String(session.selected_product_id))
      if (session?.source_context?.type === 'tiktok_shop_video_analysis') {
        setTab('factory')
        setNotice(session?.attachments?.some((item) => item.kind === 'reference_video')
          ? '视频分析报告和本地参考视频已导入。请核对并选择内容工厂中的权威商品，然后把草稿发送给制片助理。'
          : '视频分析报告已导入；本地参考视频未命中。请核对并选择内容工厂中的权威商品，然后把草稿发送给制片助理。')
      }
    }).catch((err) => {
      if (cancelled) return
      if (Number(err?.response?.status || 0) === 404) {
        const nextKey = newProducerSessionKey()
        setProducerSessionKey(nextKey)
        setProducerSessionLoadError('')
        try { window.localStorage.setItem(producerStorageKey, nextKey) } catch (_) { /* ignore */ }
        return
      }
      // A temporary API/network failure must never orphan an existing intake
      // conversation by silently replacing its locally saved session key.
      setProducerSessionLoadError('暂时无法恢复这次对话。会话编号已保留，不会新建或覆盖；请重试恢复。')
    })
    return () => { cancelled = true }
  }, [wid, producerStorageKey, producerSessionReloadNonce, handoffSessionKey])

  useEffect(() => {
    if (!wid || !producerSessionKey || !producerAttachmentAnalysisPending) return undefined
    let cancelled = false
    const poll = async () => {
      try {
        const session = await fetchContentFactoryProducerSession(wid, producerSessionKey)
        if (cancelled) return
        const attachments = Array.isArray(session?.attachments) ? session.attachments : []
        const messages = Array.isArray(session?.messages) ? session.messages : []
        setProducerAttachments(attachments)
        setProducerMessages(messages)
        setProducerPersistedMessageCount(messages.length)
        setProducerProposal(session?.proposal || null)
        setProducerProposalSha(session?.proposal_sha256 || '')
        setProducerStatus(session?.status || 'idle')
        setProducerAuthoritativeScriptId(session?.authoritative_script_message_id || null)
        setProducerAuthoritativeScript(session?.authoritative_script || '')
        setProducerAuthoritativeScriptVersion(session?.authoritative_script_version || null)
        setProducerIntentSpec(session?.intent_spec || null)
        setProducerPendingDecisionId(session?.pending_decision_id || '')
        if (session?.selected_product_id) setProducerProductId(String(session.selected_product_id))
        const activeReferences = attachments.filter((item) => item?.active !== false && item?.kind === 'reference_video')
        const stillWorking = activeReferences.some((item) => (
          ['queued', 'processing'].includes(item.analysis_status)
          || ['waiting_analysis', 'queued', 'processing'].includes(item?.analysis?.producer_turn_status)
        ))
        if (!stillWorking) {
          const failed = activeReferences.find((item) => (
            item.analysis_status === 'failed'
            || item?.analysis?.multimodal_status === 'failed'
            || item?.analysis?.producer_turn_status === 'failed'
          ))
          if (failed) {
            setError(failed?.analysis?.producer_turn_status === 'failed'
              ? '视频拆解已经完成，但制片助理暂时未能回复。原请求已保留，可以直接再次发送。'
              : '对标视频下载或多模态拆解失败。请移除后重试链接，或上传本地视频文件。')
          } else {
            setNotice('对标视频的完整画面、口播、钩子、节奏、叙事和转化结构已拆解，制片助理已结合分析结果回复。')
          }
        }
      } catch (_) {
        // Keep the durable upload and retry on the next bounded polling tick.
      }
    }
    const timer = window.setInterval(poll, 3_000)
    poll()
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [wid, producerSessionKey, producerAttachmentAnalysisPending])

  useEffect(() => {
    if (!selected) return
    setProjectEditForm(projectSettings(selected))
    setRestartStage(selected.current_stage === 'COMPLETE' || !RESTART_STAGES.includes(selected.current_stage) ? 'DIRECTOR' : selected.current_stage)
    setProjectEditOpen(false)
    setCharacterUploadOpen(false)
    setProjectCharacterDraft(newCharacterDraft(characterGroupsFromAssets(selected.assets || []).length + 1))
  }, [selected?.project_key])

  useEffect(() => {
    if (!selectedProduct) return
    setProductEditForm({
      brand_name: selectedProduct.brand_name || '',
      product_name: selectedProduct.product_name || '',
      market: selectedProduct.market || 'US',
      product_brief: selectedProduct.product_brief || '',
    })
  }, [selectedProduct?.id, selectedProduct?.updated_at])

  const refresh = useCallback(async () => {
    try {
      const [items, productItems, bridgeState] = await Promise.all([
        fetchContentFactoryProjects(wid),
        fetchContentFactoryProducts(wid),
        fetchContentFactoryBridge(wid),
      ])
      setProjects(items)
      setProducts(productItems)
      setBridge(bridgeState)
      if (!selectedKey && items[0]) setSelectedKey(items[0].project_key)
      if (productItems[0]) {
        setSelectedProductId((current) => current || String(productItems[0].id))
      }
      setError('')
    } catch (err) {
      setError(err?.message || '内容工厂状态加载失败。')
    }
  }, [wid, selectedKey, selectedProductId])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => {
    let cancelled = false
    getAiVideoProviderStatus(wid).then((status) => {
      if (cancelled) return
      setVideoModels(Array.isArray(status?.models) ? status.models : [])
      setVideoModelsError('')
    }).catch(() => {
      if (cancelled) return
      setVideoModelsError('视频模型能力暂时加载失败，请刷新后再创建项目。')
    })
    return () => { cancelled = true }
  }, [wid])
  useEffect(() => {
    const interval = selected && ['queued', 'running', 'generating_video'].includes(selected.status) ? 5000 : 15000
    const timer = window.setInterval(refresh, interval)
    return () => window.clearInterval(timer)
  }, [selected?.status, refresh])

  function downloadAgent(blob) {
    if (!blob) return false
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'MYUPONA-HermesBridge.exe'
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.URL.revokeObjectURL(url)
    return true
  }

  async function installBridgeAgent(event, requestedDevice = null) {
    event?.preventDefault?.()
    event?.stopPropagation?.()
    setBridgeBusy(true); setBridgeMessage('正在生成当前用户和设备专用的 Windows 浏览器桥...'); setError('')
    try {
      const blob = await downloadContentFactoryBridgeAgent(wid, {
        deviceId: requestedDevice?.device_id || getBridgeDeviceId(wid),
        deviceName: requestedDevice?.device_name || bridgeForm.device_name || navigator.platform || 'Windows device',
      })
      if (!downloadAgent(blob)) throw new Error('Windows 浏览器桥生成失败，请刷新后重试。')
      setBridgeMessage('已下载 MYUPONA-HermesBridge.exe。只需运行一次；以后它会自动启动，并按项目动态创建或回收独立 slot。')
    } catch (err) {
      const message = err?.message || 'Windows 浏览器桥生成失败。'
      setBridgeMessage(`生成失败：${message}`)
      setError(message)
    } finally {
      setBridgeBusy(false)
    }
  }

  async function manageBridgeDevice(action, deviceId) {
    setBridgeBusy(true); setBridgeMessage(''); setError('')
    try {
      let next
      if (action === 'bind') next = await bindContentFactoryBridgeDevice(wid, deviceId)
      else if (action === 'select') next = await selectContentFactoryBridgeDevice(wid, deviceId)
      else next = await unbindContentFactoryBridgeDevice(wid, deviceId)
      setBridge(next)
    } catch (err) {
      setError(err?.message || '设备操作失败。')
    } finally {
      setBridgeBusy(false)
    }
  }

  async function manageBridgeSlot(action, bridgeId = '') {
    if (!selectedBridgeDeviceId) {
      setError('请先选择一台在线设备。')
      return
    }
    setBridgeBusy(true); setBridgeMessage(''); setError('')
    try {
      const next = action === 'add'
        ? await prepareContentFactoryBridgeSlot(wid, selectedBridgeDeviceId)
        : await removeContentFactoryBridgeSlot(wid, bridgeId)
      setBridge(next)
      setBridgeMessage(action === 'add'
        ? 'Slot 已创建。请在新打开的 Chrome 窗口登录 ChatGPT，登录状态会自动同步。'
        : '空闲 Slot 已删除。')
    } catch (err) {
      setError(err?.message || 'Slot 操作失败。')
    } finally {
      setBridgeBusy(false)
    }
  }

  async function resetProducerConversation() {
    if (producerBusy || producerUploadBusy) return
    const nextKey = newProducerSessionKey()
    setProducerSessionKey(nextKey)
    setProducerInput('')
    setProducerMessages([])
    setProducerPersistedMessageCount(0)
    setProducerAttachments([])
    setProducerRetryTurnId('')
    setProducerRetryMessage('')
    setProducerProposal(null)
    setProducerProposalSha('')
    setProducerStatus('idle')
    setProducerAuthoritativeScriptId(null)
    setProducerAuthoritativeScript('')
    setProducerAuthoritativeScriptVersion(null)
    setProducerIntentSpec(null)
    setProducerPendingDecisionId('')
    setProducerProgress('')
    setProducerBriefOpen(false)
    setWorkspaceMode('conversation')
    if (producerStorageKey) {
      try { window.localStorage.setItem(producerStorageKey, nextKey) } catch (_) { /* ignore */ }
    }
    if (handoffSessionKey) {
      const nextParams = new URLSearchParams(searchParams)
      nextParams.delete('producer_session')
      nextParams.delete('source')
      setSearchParams(nextParams, { replace: true })
    }
  }

  function continueProducerConversationForProject(project) {
    const sessionKey = String(project?.config_json?.producer_intake?.session_key || '').trim()
    if (!sessionKey || producerBusy || producerUploadBusy) return
    setProducerSessionKey(sessionKey)
    setProducerInput('')
    setProducerMessages([])
    setProducerPersistedMessageCount(0)
    setProducerAttachments([])
    setProducerProposal(null)
    setProducerProposalSha('')
    setProducerStatus('idle')
    setProducerIntentSpec(null)
    setProducerPendingDecisionId('')
    setProducerSessionLoadError('')
    if (producerStorageKey) {
      try { window.localStorage.setItem(producerStorageKey, sessionKey) } catch (_) { /* ignore */ }
    }
    if (handoffSessionKey) {
      const nextParams = new URLSearchParams(searchParams)
      nextParams.delete('producer_session')
      nextParams.delete('source')
      setSearchParams(nextParams, { replace: true })
    }
    setProducerSessionReloadNonce((value) => value + 1)
    setNotice(`已恢复项目“${project.title}”的制片对话。可以追加视频、修改要求或制作新版本；原项目成果不会被覆盖。`)
    focusProducerComposer()
  }

  function handleProducerComposerKeyDown(event) {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent?.isComposing) return
    event.preventDefault()
    if (producerBusy || producerUploadBusy || producerAttachmentAnalysisPending || !producerInput.trim()) return
    event.currentTarget.form?.requestSubmit?.()
  }

  function changeProducerProduct(value) {
    setProducerProductId(value)
    // The displayed proposal belongs to the previous product choice. Make the
    // boundary visible and require a fresh producer turn before confirmation.
    if (producerProposal || producerProposalSha) {
      setProducerProposal(null)
      setProducerProposalSha('')
      setProducerStatus('idle')
      setProducerIntentSpec(null)
      setProducerPendingDecisionId('')
      setProducerMessages((current) => [...current, {
        role: 'assistant',
        content: '商品选择已变化。请补充一句新要求，我会按当前选择重新整理方案。',
      }])
    }
  }

  function applyProducerQuickStart(prompt) {
    if (producerBusy) return
    setProducerInput((current) => {
      const existing = current.trim()
      return existing ? `${existing}\n${prompt}` : prompt
    })
  }

  async function uploadProducerAttachments(event, assetKind) {
    const files = Array.from(event.target.files || [])
    event.target.value = ''
    if (!files.length || producerUploadBusy || producerBusy) return
    const sessionKey = producerSessionKey || newProducerSessionKey()
    setProducerSessionKey(sessionKey)
    if (producerStorageKey) {
      try { window.localStorage.setItem(producerStorageKey, sessionKey) } catch (_) { /* ignore */ }
    }
    setProducerUploadBusy(true); setError('')
    try {
      const uploaded = await uploadContentFactoryProducerAttachments(
        wid,
        sessionKey,
        files,
        assetKind,
      )
      setProducerAttachments((current) => [...current, ...uploaded])
      setProducerProposal(null)
      setProducerProposalSha('')
      setProducerStatus('idle')
      setProducerIntentSpec(null)
      setProducerPendingDecisionId('')
      if (assetKind === 'supporting_material') {
        setProducerInput((current) => current.trim() ? current : '请阅读并理解我刚上传的资料，完整提取其中的锁定要求、逐条交付内容和验收标准，再整理成可确认的制作方案。')
      }
      if (uploaded.some((item) => ['queued', 'processing'].includes(item.analysis_status))) {
        setNotice('对标视频已保存，正在提取关键画面和口播文案。分析完成后制片助理才会使用它。')
      } else {
        setNotice(`已上传 ${uploaded.length} 个${attachmentKindLabel(assetKind)}。发送下一句话后，制片助理会结合附件一起理解。`)
      }
    } catch (err) {
      setError(err?.message || '附件上传失败。')
    } finally {
      setProducerUploadBusy(false)
    }
  }

  async function removeProducerAttachment(attachmentKey) {
    if (!attachmentKey || producerUploadBusy || producerBusy) return
    setProducerUploadBusy(true); setError('')
    try {
      await deleteContentFactoryProducerAttachment(wid, producerSessionKey, attachmentKey)
      setProducerAttachments((current) => current.filter((item) => item.attachment_key !== attachmentKey))
      setProducerProposal(null)
      setProducerProposalSha('')
      setProducerStatus('idle')
      setNotice('附件已移除。请继续对话，让制片助理按当前附件重新整理方案。')
    } catch (err) {
      setError(err?.message || '附件移除失败。')
    } finally {
      setProducerUploadBusy(false)
    }
  }

  async function sendProducerMessage(event) {
    event?.preventDefault?.()
    const message = producerInput.trim()
    if (!message || producerBusy || producerUploadBusy || producerAttachmentAnalysisPending) return
    if (
      producerStatus === 'proposal_ready'
      && producerProposalSha
      && producerPendingDecisionId
      && isProducerConfirmationMessage(message)
    ) {
      setProducerInput('')
      setProducerMessages((current) => [...current, { role: 'user', content: message }])
      await confirmProducerProject({ confirmationMessage: message })
      return
    }
    const sessionKey = producerSessionKey || newProducerSessionKey()
    setProducerSessionKey(sessionKey)
    if (producerStorageKey) {
      try { window.localStorage.setItem(producerStorageKey, sessionKey) } catch (_) { /* ignore */ }
    }
    const benchmarkUrl = benchmarkVideoUrlFromText(message)
    if (benchmarkUrl) {
      setProducerUploadBusy(true)
      setProducerProgress('正在安全下载公开对标视频，随后会提取口播并进行完整多模态拆解…')
      setError('')
      try {
        const attachment = await addContentFactoryProducerReferenceLink(
          wid,
          sessionKey,
          {
            url: benchmarkUrl,
            contextMessage: message,
            productId: producerProductId ? Number(producerProductId) : null,
          },
        )
        setProducerAttachments((current) => {
          const next = current.map((item) => item.kind === 'reference_video'
            ? { ...item, active: false }
            : item)
          const existingIndex = next.findIndex((item) => item.attachment_key === attachment.attachment_key)
          if (existingIndex >= 0) next[existingIndex] = attachment
          else next.push(attachment)
          return next
        })
        setProducerMessages((current) => [...current, { role: 'user', content: message }])
        setProducerInput('')
        setProducerProposal(null)
        setProducerProposalSha('')
        setProducerStatus('idle')
        setProducerIntentSpec(null)
        setProducerPendingDecisionId('')
        setNotice('已识别爆款视频链接。系统会在后台下载并做逐段多模态拆解，完成后制片助理自动继续回复，不需要重复发送。')
        return
      } catch (err) {
        setError(producerErrorMessage(err))
        return
      } finally {
        setProducerProgress('')
        setProducerUploadBusy(false)
      }
    }
    const startedAt = Date.now()
    const baselineMessageCount = producerPersistedMessageCount
    const clientTurnId = producerRetryMessage === message && producerRetryTurnId
      ? producerRetryTurnId
      : newProducerTurnId()
    setProducerBusy(true); setError('')
    setProducerProgress('制片助理正在理解需求并整理方案…')
    setProducerMessages((current) => [...current, { role: 'user', content: message }])
    setProducerInput('')
    const slowTimer = window.setTimeout(() => {
      setProducerProgress('正在深入分析文案、受众和制作方案，复杂需求可能需要 1–3 分钟，请不要重复提交…')
    }, 12_000)
    try {
      const result = await sendContentFactoryProducerTurn(wid, {
        session_key: sessionKey,
        client_turn_id: clientTurnId,
        message,
        product_id: producerProductId ? Number(producerProductId) : null,
      })
      setProducerSessionKey(result.session_key || sessionKey)
      setProducerMessages((current) => [...current, { role: 'assistant', content: result.assistant_message }])
      setProducerPersistedMessageCount(baselineMessageCount + 2)
      setProducerRetryTurnId('')
      setProducerRetryMessage('')
      setProducerProposal(result.proposal || null)
      setProducerProposalSha(result.proposal_sha256 || '')
      setProducerStatus(result.status || 'needs_input')
      setProducerAuthoritativeScriptId(result.authoritative_script_message_id || null)
      setProducerAuthoritativeScript(result.authoritative_script || '')
      setProducerAuthoritativeScriptVersion(result.authoritative_script_version || null)
      setProducerIntentSpec(result.intent_spec || null)
      setProducerPendingDecisionId(result.pending_decision_id || '')
      if (result.selected_product_id) setProducerProductId(String(result.selected_product_id))
      if (
        result.status === 'proposal_ready'
        && result.proposal_sha256
        && result.pending_decision_id
        && isProducerExecutionMessage(message)
      ) {
        try {
          const project = await confirmContentFactoryProducerProject(
            wid,
            result.session_key || sessionKey,
            result.proposal_sha256,
            result.pending_decision_id,
          )
          setProducerStatus('created')
          setProducerPendingDecisionId('')
          setProducerMessages((current) => [...current, {
            role: 'assistant',
            content: `已识别你的明确执行要求，项目“${project.title}”已创建并开始执行。后续可以继续在这里追加或修改。`,
          }])
          setSelectedKey(project.project_key)
          await refresh()
          setSelectedKey(project.project_key)
          setWorkspaceMode('production')
        } catch (confirmError) {
          // The reviewed proposal remains durable and visible. A transient
          // creation failure must never erase or resend the successful model
          // turn; the user can use the explicit confirmation button safely.
          setError(confirmError?.message || '方案已整理完成，但项目创建暂时失败，请点击确认按钮重试。')
        }
      }
    } catch (err) {
      let recovered = null
      if (producerRequestMayStillBeRunning(err)) {
        setProducerProgress('网络连接中断，正在同步这次已提交的回复，不会重复发送…')
        recovered = await recoverProducerTurn(wid, sessionKey, baselineMessageCount, startedAt)
      }
      if (recovered) {
        const messages = Array.isArray(recovered?.messages) ? recovered.messages : []
        setProducerMessages(messages)
        setProducerPersistedMessageCount(messages.length)
        setProducerAttachments(Array.isArray(recovered?.attachments) ? recovered.attachments : producerAttachments)
        setProducerProposal(recovered?.proposal || null)
        setProducerProposalSha(recovered?.proposal_sha256 || '')
        setProducerStatus(recovered?.status || 'needs_input')
        setProducerAuthoritativeScriptId(recovered?.authoritative_script_message_id || null)
        setProducerAuthoritativeScript(recovered?.authoritative_script || '')
        setProducerAuthoritativeScriptVersion(recovered?.authoritative_script_version || null)
        setProducerIntentSpec(recovered?.intent_spec || null)
        setProducerPendingDecisionId(recovered?.pending_decision_id || '')
        setProducerRetryTurnId('')
        setProducerRetryMessage('')
        if (recovered?.selected_product_id) setProducerProductId(String(recovered.selected_product_id))
        if (
          recovered?.status === 'proposal_ready'
          && recovered?.proposal_sha256
          && recovered?.pending_decision_id
          && isProducerExecutionMessage(message)
        ) {
          try {
            const project = await confirmContentFactoryProducerProject(
              wid,
              recovered.session_key || sessionKey,
              recovered.proposal_sha256,
              recovered.pending_decision_id,
            )
            setProducerStatus('created')
            setProducerPendingDecisionId('')
            setProducerMessages([...messages, {
              role: 'assistant',
              content: `已同步制片回复，并识别你的明确执行要求；项目“${project.title}”已创建并开始执行。后续可以继续在这里追加或修改。`,
            }])
            setSelectedKey(project.project_key)
            await refresh()
            setSelectedKey(project.project_key)
            setWorkspaceMode('production')
          } catch (confirmError) {
            setError(confirmError?.message || '方案已同步完成，但项目创建暂时失败，请点击确认按钮重试。')
          }
        }
      } else {
        setProducerMessages((current) => {
          const last = current[current.length - 1]
          return last?.role === 'user' && last?.content === message ? current.slice(0, -1) : current
        })
        setProducerRetryTurnId(clientTurnId)
        setProducerRetryMessage(message)
        setProducerInput(message)
        setError(producerErrorMessage(err))
      }
    } finally {
      window.clearTimeout(slowTimer)
      setProducerProgress('')
      setProducerBusy(false)
    }
  }

  async function confirmProducerProject({ confirmationMessage = '' } = {}) {
    if (!producerSessionKey || !producerProposalSha || !producerPendingDecisionId || producerStatus !== 'proposal_ready') return
    setProducerBusy(true); setError('')
    try {
      const project = await confirmContentFactoryProducerProject(
        wid,
        producerSessionKey,
        producerProposalSha,
        producerPendingDecisionId,
      )
      setProducerStatus('created')
      setProducerPendingDecisionId('')
      setProducerMessages((current) => [...current, {
        role: 'assistant',
        content: `${confirmationMessage ? '已按你的确认执行。' : ''}项目“${project.title}”已创建并开始执行。后续可以直接在右侧查看进度。`,
      }])
      setSelectedKey(project.project_key)
      await refresh()
      setSelectedKey(project.project_key)
      setWorkspaceMode('production')
    } catch (err) {
      setError(err?.message || '项目创建失败。')
    } finally {
      setProducerBusy(false)
    }
  }

  async function createProduct(event) {
    event.preventDefault()
    setBusy(true); setError('')
    try {
      const product = await createContentFactoryProduct(wid, productForm)
      setProductForm(emptyProductForm)
      setSelectedProductId(String(product.id))
      await refresh()
    } catch (err) { setError(err?.message || '商品创建失败。') } finally { setBusy(false) }
  }

  async function uploadProductFiles(event) {
    if (!selectedProduct || !event.target.files?.length) return
    setBusy(true); setError('')
    try { await uploadContentFactoryProductAssets(wid, selectedProduct.id, event.target.files); await refresh() }
    catch (err) { setError(err?.message || '商品资料上传失败。') }
    finally { event.target.value = ''; setBusy(false) }
  }

  async function saveProductUpdate(productId = selectedProduct?.id) {
    if (!productId) return
    setBusy(true); setError('')
    try {
      const product = await updateContentFactoryProduct(wid, productId, productEditForm)
      setSelectedProductId(String(product.id))
      await refresh()
    } catch (err) { setError(err?.message || '商品更新失败。') }
    finally { setBusy(false) }
  }

  async function deleteProductAsset(productId, asset) {
    if (!productId || !asset) return
    if (!window.confirm(`确定删除资料“${asset.original_name}”吗？删除后需要重新生成产品事实。`)) return
    setBusy(true); setError('')
    try {
      await deleteContentFactoryProductAsset(wid, productId, asset.id)
      await refresh()
    } catch (err) { setError(err?.message || '商品资料删除失败。') }
    finally { setBusy(false) }
  }

  async function deleteProduct(product) {
    if (!product) return
    if (!window.confirm(`确定删除商品“${product.brand_name} · ${product.product_name}”及商品库源资料吗？已有项目和成品不会被删除。`)) return
    setBusy(true); setError('')
    try {
      await deleteContentFactoryProduct(wid, product.id)
      if (String(selectedProductId) === String(product.id)) {
        setSelectedProductId('')
      }
      await refresh()
    } catch (err) { setError(err?.message || '商品删除失败。') }
    finally { setBusy(false) }
  }

  async function generateProductFacts(productId = selectedProduct?.id) {
    if (!productId) return
    setBusy(true); setError('')
    try { await generateContentFactoryProductFacts(wid, productId); await refresh() }
    catch (err) { setError(err?.message || '产品事实生成启动失败。') }
    finally { setBusy(false) }
  }

  async function uploadFiles(event, assetKind = 'source') {
    if (!selected || !event.target.files?.length) return
    setBusy(true); setError('')
    try { await uploadContentFactoryAssets(wid, selected.project_key, event.target.files, assetKind); await refresh() }
    catch (err) { setError(err?.message || '文件上传失败。') }
    finally { event.target.value = ''; setBusy(false) }
  }

  async function uploadProjectCharacter() {
    if (!selected || !projectCharacterDraft.files.length) {
      setError('请先为该人物选择至少一张图片。')
      return
    }
    const existingCount = selectedCharacterGroups.reduce((total, group) => total + group.files.length, 0)
    if (existingCount + projectCharacterDraft.files.length > 16) {
      setError('每个项目最多上传 16 张人物锚点图。')
      return
    }
    setBusy(true); setError('')
    try {
      await uploadContentFactoryAssets(
        wid,
        selected.project_key,
        projectCharacterDraft.files,
        'character_reference',
        {
          character_key: projectCharacterDraft.key,
          character_name: projectCharacterDraft.name,
          character_description: projectCharacterDraft.description,
        },
      )
      setProjectCharacterDraft(newCharacterDraft(selectedCharacterGroups.length + 2))
      await refresh()
    } catch (err) {
      setError(err?.message || '人物锚点图上传失败。')
    } finally {
      setBusy(false)
    }
  }

  async function deleteProject() {
    if (!selected || !window.confirm(`确定删除项目“${selected.title}”及其内容工厂资料吗？`)) return
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await deleteContentFactoryProject(wid, selected.project_key)
      setNotice(result?.cleanup_pending
        ? '项目已从列表移除，已提交的供应商任务完成后会自动清理剩余文件。'
        : '项目及其专属文件已删除。')
      setSelectedKey('')
      await refresh()
    } catch (err) { setError(err?.message || '项目删除失败。') } finally { setBusy(false) }
  }

  async function pauseProject() {
    if (!selected) return
    setBusy(true); setError('')
    try {
      await pauseContentFactoryProject(wid, selected.project_key, '用户手动暂停，等待人工干预。')
      await refresh()
    } catch (err) { setError(err?.message || '项目暂停失败。') } finally { setBusy(false) }
  }

  async function resumeProject() {
    if (!selected) return
    setBusy(true); setError('')
    try {
      await resumeContentFactoryProject(wid, selected.project_key)
      await refresh()
    } catch (err) { setError(err?.message || '项目恢复失败。') } finally { setBusy(false) }
  }

  async function saveProjectSettings(restart = false) {
    if (!selected || !projectEditForm) return
    if (projectEditForm.video_duration_min_seconds > projectEditForm.video_duration_max_seconds) {
      setError('最短时长不能大于最长时长。')
      return
    }
    if (['queued', 'running', 'generating_video'].includes(selected.status)) {
      setError('项目正在运行，请先暂停项目，再编辑或重新开始。')
      return
    }
    if (restart && !window.confirm(`保存修改并从“${LABELS[restartStage] || restartStage}”重新开始？该阶段及其下游旧结果会被替换。`)) return
    setBusy(true); setError('')
    try {
      await updateContentFactoryProject(wid, selected.project_key, projectEditForm)
      if (restart) {
        await restartContentFactoryProject(wid, selected.project_key, {
          stage: restartStage,
          instruction: instruction || 'Use the edited saved project settings and restart unattended.',
          autoRun: projectEditForm.auto_run,
        })
        setInstruction('')
      }
      setProjectEditOpen(false)
      await refresh()
    } catch (err) { setError(err?.message || '项目设置保存失败。') } finally { setBusy(false) }
  }

  async function runStage(runMode = 'continue') {
    if (!selected) return
    setBusy(true); setError('')
    try {
      await runContentFactoryStage(wid, selected.project_key, {
        instruction,
        stage: targetStage || selected.current_stage,
        runMode,
      })
      setInstruction(''); await refresh()
    } catch (err) { setError(err?.message || '阶段启动失败。') } finally { setBusy(false) }
  }

  const paused = selected?.status === 'paused'
  const canRun = selected && !busy && !['queued', 'running', 'generating_video', 'paused'].includes(selected.status)
  const videoState = selected?.state_json || {}
  const videoGroups = Array.isArray(videoState.ai_video_group_statuses) ? videoState.ai_video_group_statuses : []
  const composedVideoCount = Number(videoState.ai_video_composed_video_count || 0)
  const pendingVideoTaskCount = Array.isArray(videoState.ai_video_pending_task_ids) ? videoState.ai_video_pending_task_ids.length : 0
  const failedVideoTaskCount = Array.isArray(videoState.ai_video_failed_task_ids) ? videoState.ai_video_failed_task_ids.length : 0
  const deliverables = selected?.deliverables || {}
  const deliverableItems = Array.isArray(deliverables.items) ? deliverables.items : []
  const deliverableTargetCount = Number(deliverables.target_count || selected?.config_json?.video_count || 0)
  const deliverableCompleteCount = Number(deliverables.complete_count || 0)
  const projectComposedVideoCount = Math.max(composedVideoCount, deliverableCompleteCount)
  const hasDeliverableFiles = deliverableItems.some((item) => item.video || item.guidance)
  const selectedIntentManifest = selected?.config_json?.producer_intent_spec?.intent_manifest || null
  const recoveryDecision = videoState.last_recovery_supervisor_decision || null
  const directorApproved = Object.keys(videoState.approved_director_artifacts_by_variant || {}).length > 0
  const productionPlanApproved = Object.keys(videoState.approved_production_plans_by_variant || {}).length > 0
  const finalIntentEvidence = {}
  for (const asset of (selected?.assets || [])) {
    if (asset?.kind !== 'video') continue
    const review = asset?.meta_json?.intent_fidelity || asset?.meta_json?.final_quality?.intent_fidelity || {}
    for (const [requirementId, evidence] of Object.entries(review.requirement_evidence || {})) {
      finalIntentEvidence[requirementId] = evidence
    }
  }

  return (
    <div className="content-factory-page">
      <div className="content-factory-page__header">
        <div>
          <h2>内容工厂</h2>
          <p className="muted">从需求沟通到成片交付的一站式 AI 制作空间</p>
        </div>
        <div className="content-factory-system-pill" style={bridgeTone(bridge)}>
          <span className="content-factory-system-pill__dot" aria-hidden="true" />
          {bridgeStatusText(bridge)}
        </div>
      </div>
      <details className="card content-factory-system-drawer">
        <summary>
          <span>
            <strong>浏览器与设备</strong>
            <small>API 优先；只有需要浏览器兜底或维护登录时才使用 Slot</small>
          </span>
          <span className="content-factory-system-drawer__summary-status">
            {bridge.active_slots || 0}/{bridge.capacity || 1} 使用中
          </span>
        </summary>
        <div className="content-factory-system-drawer__body">
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
          <div>
            <strong>当前设备 Slot 池</strong>
            <div className="muted" style={{ marginTop: 4 }}>
              动态模式 · active {bridge.active_slots || 0}/{bridge.capacity || 1}
              {bridge.load ? ` · load ${bridge.load.load1}/${bridge.load.cpu_count}` : ''}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            <button className="btn secondary" type="button" onClick={() => manageBridgeSlot('add')} disabled={bridgeBusy || !selectedBridgeDeviceId}>
              新增登录 Slot
            </button>
            <button className="btn" type="button" onClick={(event) => installBridgeAgent(event)} disabled={bridgeBusy}>
              {bridgeBusy ? '生成中...' : '下载 Windows 浏览器桥'}
            </button>
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 8 }}>
          <input placeholder="设备名称" value={bridgeForm.device_name} onChange={(event) => setBridgeForm({ ...bridgeForm, device_name: event.target.value })} />
          <div className="muted" style={{ gridColumn: 'span 2', alignSelf: 'center' }}>运行一次 EXE 即可。Agent 会随登录自启动，并按项目动态创建独立 Chrome slot。</div>
        </div>
        {bridgeMessage ? (
          <div style={{ ...statusTone(bridgeMessage.startsWith('注册失败') ? 'failed' : 'running'), padding: '7px 10px', borderRadius: 6, fontSize: 13 }}>{bridgeMessage}</div>
        ) : null}
        {!bridge.connected && bridge?.detail ? (
          <div style={{ ...bridgeTone(bridge), padding: '8px 10px', borderRadius: 6, fontSize: 13 }}>
            {bridge.detail}
          </div>
        ) : null}
        {bridge?.selection_required ? (
          <div style={{ ...statusTone('paused'), padding: '8px 10px', borderRadius: 6, fontSize: 13, fontWeight: 600 }}>
            多台已绑定设备在线。请选择一台当前设备，后续新项目会固定使用该设备。
          </div>
        ) : null}
        {Array.isArray(bridge?.devices) && bridge.devices.length ? (
          <div style={{ display: 'grid', gap: 6 }}>
            {bridge.devices.map((device) => {
              const activeCount = Array.isArray(device.active_project_ids) ? device.active_project_ids.length : 0
              const isCurrentDevice = String(device.device_id) === selectedBridgeDeviceId
              const versionMismatch = Boolean(device.agent_update_required)
              const updateFailed = device.agent_update_state === 'failed'
              return (
                <div key={device.device_id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6, background: isCurrentDevice ? '#eef5ff' : '#fff', flexWrap: 'wrap' }}>
                  <div style={{ minWidth: 220 }}>
                    <strong>{device.device_name || 'Windows device'}</strong>
                    <span style={{ marginLeft: 8, fontSize: 12, color: device.online ? '#167a3f' : '#6b7280' }}>{device.online ? '在线' : '离线'}</span>
                    {device.connected ? <span style={{ marginLeft: 6, fontSize: 12, color: '#167a3f' }}>CDP 已连接</span> : null}
                    <div className="muted" title={device.device_id} style={{ marginTop: 3, fontSize: 11 }}>{device.device_id} · {device.slot_count || 0} 个 slot{activeCount ? ` · ${activeCount} 个项目运行中` : ''}</div>
                    <div className="muted" style={{ marginTop: 3, fontSize: 11 }}>
                      客户端 {device.agent_version || '未知'} · 服务器 {device.server_agent_version || bridge.server_agent_version || '未知'}
                    </div>
                    {versionMismatch ? (
                      <div style={{ marginTop: 4, fontSize: 12, color: updateFailed ? '#b42318' : '#9a5b00' }}>
                        {updateFailed
                          ? `自动更新失败${device.agent_update_error ? `：${device.agent_update_error}` : ''}`
                          : (device.online ? '版本不一致，客户端正在自动更新。' : '版本不一致且客户端离线，请手动更新。')}
                      </div>
                    ) : null}
                  </div>
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                    {isCurrentDevice ? <span style={{ ...statusTone('running'), padding: '5px 8px', borderRadius: 999, fontSize: 12 }}>当前设备</span> : null}
                    {!device.bound ? (
                      <button className="btn secondary" type="button" onClick={() => manageBridgeDevice('bind', device.device_id)} disabled={bridgeBusy}>绑定</button>
                    ) : null}
                    {device.bound && device.online && !isCurrentDevice ? (
                      <button className="btn secondary" type="button" onClick={() => manageBridgeDevice('select', device.device_id)} disabled={bridgeBusy}>选用此设备</button>
                    ) : null}
                    {device.bound ? (
                      <button className="btn secondary" type="button" onClick={() => manageBridgeDevice('unbind', device.device_id)} disabled={bridgeBusy || activeCount > 0}>解绑</button>
                    ) : null}
                    {versionMismatch ? (
                      <button className="btn" type="button" onClick={(event) => installBridgeAgent(event, device)} disabled={bridgeBusy}>
                        下载最新版 EXE
                      </button>
                    ) : null}
                  </div>
                </div>
              )
            })}
          </div>
        ) : null}
        <div className="muted">Slot 只属于当前用户的当前设备，不会跨电脑继承。每个并行项目固定占用一个已登录 Slot；请先在对应 Chrome 窗口手动登录 ChatGPT。</div>
      {currentDeviceSlots.length ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 8 }}>
          {currentDeviceSlots.map((slot) => {
            const tone = slotTone(slot)
            const activeProject = slot.active_project
            return (
              <div key={slot.slot} style={{ border: `1px solid ${tone.borderColor}`, borderRadius: 8, padding: 10, background: tone.background, color: tone.color }}>
	                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' }}>
	                  <strong>Slot {slot.slot}</strong>
	                  <span style={{ fontSize: 12, fontWeight: 700 }}>
	                    {slotStatusText(slot, activeProject)}
	                  </span>
	                </div>
	                <div style={{ marginTop: 5, fontSize: 12, color: '#4b5563', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
	                  {activeProject ? `${activeProject.title} · ${LABELS[activeProject.stage] || activeProject.stage}` : '暂停、删除、失败项目不占用 slot'}
	                </div>
	                {!slot.connected && slot.agent_error ? (
	                  <div title={slot.agent_error} style={{ marginTop: 5, fontSize: 12, color: '#92400e', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
	                    CDP：{slot.agent_error}
	                  </div>
	                ) : null}
	                {!slot.connected && slot.agent_last_heartbeat_at ? (
	                  <div style={{ marginTop: 4, fontSize: 11, color: '#6b7280' }}>
	                    桥程序心跳：{String(slot.agent_last_heartbeat_at).replace('T', ' ').slice(0, 19)}
	                  </div>
	                ) : null}
	                <div style={{ marginTop: 4, fontSize: 11, color: '#6b7280' }}>
	                  队列 {slot.queue_depth || 0} · {slot.url || ''}
	                </div>
	                {slot.account_name ? <div style={{ marginTop: 4, fontSize: 11, color: '#6b7280' }}>账号：{slot.account_name}</div> : null}
	                {!activeProject ? (
	                  <button className="btn secondary" type="button" style={{ marginTop: 8 }} onClick={() => manageBridgeSlot('remove', slot.bridge_id)} disabled={bridgeBusy}>
	                    删除空闲 Slot
	                  </button>
	                ) : null}
              </div>
            )
          })}
        </div>
      ) : (
        <div className="muted">当前设备还没有 Slot。点击“新增登录 Slot”，然后在自动打开的 Chrome 窗口登录 ChatGPT。</div>
      )}
        </div>
      </details>
      <div className="content-factory-tabs" role="tablist" aria-label="内容工厂主导航">
        <button className={tab === 'factory' ? 'btn' : 'btn secondary'} type="button" onClick={() => setTab('factory')}>内容工厂</button>
        <button className={tab === 'products' ? 'btn' : 'btn secondary'} type="button" onClick={() => setTab('products')}>商品库</button>
      </div>
      {error ? <div className="alert error">{error}</div> : null}
      {notice ? <div className="alert">{notice}</div> : null}
      {tab === 'products' ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(260px, 360px) minmax(0, 1fr)', gap: 14 }}>
          <aside className="card" style={{ padding: 14, alignSelf: 'start' }}>
            <h3 style={{ marginTop: 0 }}>新增商品</h3>
            <form onSubmit={createProduct} style={{ display: 'grid', gap: 8 }}>
              <input className="input" placeholder="品牌名称" value={productForm.brand_name} onChange={(e) => setProductForm({ ...productForm, brand_name: e.target.value })} required />
              <input className="input" placeholder="产品名称" value={productForm.product_name} onChange={(e) => setProductForm({ ...productForm, product_name: e.target.value })} required />
              <input className="input" placeholder="市场，如 US" value={productForm.market} onChange={(e) => setProductForm({ ...productForm, market: e.target.value })} required />
              <textarea className="input" rows={4} placeholder="稳定商品属性补充（可选）；不要填价格、促销、包邮或视频创意" value={productForm.product_brief} onChange={(e) => setProductForm({ ...productForm, product_brief: e.target.value })} />
              <div className="muted" style={{ fontSize: 12 }}>价格、优惠和内容要求属于具体项目，由 AI 制片助理在对话中确认，不写入公司商品库。</div>
              <button className="btn" disabled={busy}>保存商品</button>
            </form>
          </aside>
          <main className="card" style={{ padding: 14, minWidth: 0 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <h3 style={{ margin: 0 }}>公司商品库</h3>
              <button className="btn secondary" type="button" onClick={refresh}>刷新</button>
            </div>
            <div style={{ display: 'grid', gap: 10, marginTop: 14 }}>
              {products.map((product) => {
                const active = String(product.id) === String(selectedProductId)
                const factsStatus = product.meta_json?.facts_status || (product.facts_json ? 'success' : 'idle')
                return (
                  <section key={product.id} style={{ border: active ? '1px solid #3b82f6' : '1px solid var(--border)', borderRadius: 8, padding: 12, background: active ? '#f7fbff' : '#fff' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
                      <button type="button" onClick={() => setSelectedProductId(String(product.id))} style={{ border: 'none', background: 'transparent', padding: 0, textAlign: 'left', cursor: 'pointer' }}>
                        <strong>{product.brand_name} · {product.product_name}</strong>
                        <div className="muted" style={{ marginTop: 4 }}>{product.market} · 资料 {product.assets?.length || 0} 个</div>
                      </button>
                      <span style={{ ...statusTone(factsStatus), padding: '5px 9px', borderRadius: 999, fontSize: 12, fontWeight: 600 }}>{factsLabel(product)}</span>
                    </div>
                    {product.meta_json?.facts_error ? <div className="alert error" style={{ marginTop: 10 }}>{product.meta_json.facts_error}</div> : null}
                    {active ? (
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(120px, 1fr))', gap: 8, marginTop: 10, padding: 10, borderRadius: 6, border: '1px solid var(--border)', background: '#fff' }}>
                        <input className="input" aria-label="品牌名称" value={productEditForm.brand_name} onChange={(event) => setProductEditForm({ ...productEditForm, brand_name: event.target.value })} />
                        <input className="input" aria-label="产品名称" value={productEditForm.product_name} onChange={(event) => setProductEditForm({ ...productEditForm, product_name: event.target.value })} />
                        <input className="input" aria-label="市场" value={productEditForm.market} onChange={(event) => setProductEditForm({ ...productEditForm, market: event.target.value })} />
                        <textarea className="input" rows={2} aria-label="稳定商品属性补充" placeholder="只保存稳定商品属性，不填价格、促销、包邮或视频创意" value={productEditForm.product_brief} onChange={(event) => setProductEditForm({ ...productEditForm, product_brief: event.target.value })} style={{ gridColumn: '1 / -1' }} />
                        <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                          <button className="btn secondary" type="button" onClick={() => saveProductUpdate(product.id)} disabled={busy}>保存商品信息</button>
                          <button className="btn secondary" type="button" onClick={() => deleteProduct(product)} disabled={busy} style={{ color: '#c93636' }}>删除商品</button>
                        </div>
                      </div>
                    ) : null}
                    <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                      <label className="btn secondary" style={{ cursor: 'pointer' }}>
                        上传商品资料
                        <input type="file" multiple accept=".pdf,image/*" onChange={uploadProductFiles} disabled={busy || !active} style={{ display: 'none' }} />
                      </label>
                      <button className="btn" type="button" onClick={() => generateProductFacts(product.id)} disabled={busy || !product.assets?.length}>
                        上传完毕，生成产品事实
                      </button>
                      <button className="btn secondary" type="button" onClick={() => { setSelectedProductId(String(product.id)); changeProducerProduct(String(product.id)); setTab('factory') }}>
                        用此商品创建内容
                      </button>
                    </div>
                    {product.assets?.length ? (
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 8, marginTop: 10 }}>
                        {product.assets.map((asset) => {
                          const url = contentFactoryProductAssetUrl(wid, product.id, asset.id)
                          const isImage = asset.mime_type?.startsWith('image/')
                          return (
                            <div key={asset.id} style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 8, minWidth: 0, background: '#fff' }}>
                              {isImage ? <img src={url} alt={asset.original_name} style={{ width: '100%', height: 'auto', maxHeight: 360, objectFit: 'contain', display: 'block', background: '#f6f7f9', marginBottom: 6 }} /> : null}
                              <div title={asset.original_name} style={{ fontSize: 12, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{asset.original_name}</div>
                              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginTop: 6 }}>
                                <a href={url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>查看文件</a>
                                <button type="button" className="btn secondary" onClick={() => deleteProductAsset(product.id, asset)} disabled={busy || !active} style={{ padding: '4px 8px', fontSize: 12, color: '#c93636' }}>删除</button>
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    ) : null}
                  </section>
                )
              })}
              {!products.length ? <div className="muted">还没有商品。先在左侧新增一个商品，再上传资料。</div> : null}
            </div>
          </main>
        </div>
      ) : (
        <div className={`content-factory-workspace-layout is-${workspaceMode}-mode`}>
          <div className="content-factory-workspace-toolbar" role="tablist" aria-label="内容工作台模式">
            <div>
              <strong>{workspaceMode === 'conversation' ? '需求沟通工作台' : '项目执行工作台'}</strong>
              <span>{workspaceMode === 'conversation' ? '对话、素材和方案确认在这里完成' : '跟踪阶段、成片与交付状态'}</span>
            </div>
            <div className="content-factory-workspace-switch" role="group" aria-label="切换工作台">
              <button
                className={workspaceMode === 'conversation' ? 'is-active' : ''}
                type="button"
                onClick={() => { setWorkspaceMode('conversation'); focusProducerComposer() }}
              >
                与制片助理沟通
              </button>
              <button
                className={workspaceMode === 'production' ? 'is-active' : ''}
                type="button"
                onClick={() => setWorkspaceMode('production')}
                disabled={!selected}
              >
                查看项目执行
              </button>
            </div>
          </div>
          <nav className="card content-factory-project-rail" aria-label="内容项目">
            <div className="content-factory-project-rail__header">
              <div>
                <strong>项目</strong>
                <span>{projects.length}</span>
              </div>
              <button className="content-factory-icon-button" type="button" onClick={refresh} aria-label="刷新项目" title="刷新项目">↻</button>
            </div>
            <div className="content-factory-project-list">
              {projects.map((project) => (
                <button
                  key={project.project_key}
                  type="button"
                  className={project.project_key === selected?.project_key ? 'content-factory-project-item is-active' : 'content-factory-project-item'}
                  onClick={() => { setSelectedKey(project.project_key); setWorkspaceMode('production') }}
                >
                  <strong>{project.title}</strong>
                  <span>{projectStatusText(project)}</span>
                </button>
              ))}
              {!projects.length ? <span className="muted content-factory-empty-projects">暂无项目，先与制片助理沟通。</span> : null}
            </div>
          </nav>
          <aside ref={producerPanelRef} className="card content-factory-producer-panel">
            <section className="content-factory-producer-shell">
                <div className="content-factory-producer-header">
                  <div>
                    <strong>AI 制片助理</strong>
                    <div className="muted">
                      说清目标，其余交给助理整理
                    </div>
                  </div>
                  <span className={producerStatus === 'proposal_ready' ? 'content-factory-producer-status is-ready' : 'content-factory-producer-status'}>
                    {producerStatus === 'proposal_ready' ? '等待确认' : producerStatus === 'created' ? '已创建 · 可继续沟通' : '需求沟通中'}
                  </span>
                </div>
                {producerSessionLoadError ? (
                  <div style={{ padding: 10, border: '1px solid #e0a000', borderRadius: 8, background: '#fff8e6', display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                    <span style={{ flex: 1 }}>{producerSessionLoadError}</span>
                    <button className="btn secondary" type="button" style={{ width: 'auto', minHeight: 30, padding: '4px 9px' }} onClick={() => setProducerSessionReloadNonce((value) => value + 1)}>
                      重试恢复
                    </button>
                  </div>
                ) : null}
                <details className="content-factory-materials" defaultOpen={!producerMessages.length && !producerAttachments.length}>
                  <summary>
                    <span>项目素材与商品</span>
                    <span>{producerAttachments.length ? `${producerAttachments.length} 个附件` : producerProductId ? '已关联商品' : '可选'}</span>
                  </summary>
                  <div className="content-factory-materials__body">
                    <label className="content-factory-field">
                      <span>关联商品（可选）</span>
                      <select className="input" value={producerProductId} onChange={(event) => changeProducerProduct(event.target.value)} disabled={producerBusy}>
                        <option value="">不绑定商品</option>
                        {products.map((product) => <option key={product.id} value={product.id}>{product.brand_name} · {product.product_name}</option>)}
                      </select>
                    </label>
                  <div className="content-factory-upload-actions">
                    <label className="btn secondary" style={{ width: 'auto', cursor: producerBusy || producerUploadBusy ? 'not-allowed' : 'pointer', fontSize: 12 }}>
                      + 上传对标视频
                      <input
                        type="file"
                        accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm"
                        hidden
                        disabled={producerBusy || producerUploadBusy}
                        onChange={(event) => uploadProducerAttachments(event, 'reference_video')}
                      />
                    </label>
                    <label className="btn secondary" style={{ width: 'auto', cursor: producerBusy || producerUploadBusy ? 'not-allowed' : 'pointer', fontSize: 12 }}>
                      + 上传人物参考图
                      <input
                        type="file"
                        accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
                        multiple
                        hidden
                        disabled={producerBusy || producerUploadBusy}
                        onChange={(event) => uploadProducerAttachments(event, 'character_reference')}
                      />
                    </label>
                    <label className="btn secondary" style={{ width: 'auto', cursor: producerBusy || producerUploadBusy ? 'not-allowed' : 'pointer', fontSize: 12 }}>
                      + 上传资料 / 图片
                      <input
                        type="file"
                        accept={PRODUCER_SUPPORTING_MATERIAL_ACCEPT}
                        multiple
                        hidden
                        disabled={producerBusy || producerUploadBusy}
                        onChange={(event) => uploadProducerAttachments(event, 'supporting_material')}
                      />
                    </label>
                  </div>
                  <div className="muted content-factory-materials__help">
                    支持爆款视频链接、本地视频、人物参考图，以及 Word、Excel、PowerPoint、PDF、OpenDocument、文本和常用图片。视频将在后台完成口播与多模态拆解。
                  </div>
                  {producerUploadBusy ? <div className="muted" style={{ fontSize: 12 }}>正在接收素材；对标视频会继续在后台下载、转写并进行多模态拆解…</div> : null}
                  {producerAttachments.length ? (
                    <div style={{ display: 'grid', gap: 6 }}>
                      {producerAttachments.map((attachment) => (
                        <div key={attachment.attachment_key} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 9px', border: '1px solid var(--border)', borderRadius: 8, background: attachment.active === false ? '#f5f6f8' : '#fff', opacity: attachment.active === false ? 0.72 : 1, fontSize: 12 }}>
                          <span style={{ minWidth: 0, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={attachment.original_name}>
                            {attachmentKindLabel(attachment.kind)} · {attachment.original_name}
                          </span>
                          <span className="muted">{formatAttachmentSize(attachment.size_bytes)}</span>
                          <span className="muted">{attachmentAnalysisLabel(attachment)}</span>
                          <button
                            className="btn secondary"
                            type="button"
                            style={{ width: 'auto', minHeight: 28, padding: '3px 8px', fontSize: 12 }}
                            disabled={producerBusy || producerUploadBusy || attachment.locked}
                            onClick={() => removeProducerAttachment(attachment.attachment_key)}
                          >
                            {attachment.locked ? '已用于项目' : '移除'}
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : null}
                  </div>
                </details>
                <div ref={producerMessagesRef} className={producerMessages.length ? 'content-factory-conversation has-messages' : 'content-factory-conversation'}>
                  {producerAuthoritativeScriptId ? (
                    <div style={{ padding: '6px 8px', borderRadius: 7, background: '#e9f8ef', color: '#216e3a', fontSize: 12 }}>
                      已识别文案来源{producerAuthoritativeScriptVersion ? ` · 当前版本 v${producerAuthoritativeScriptVersion}` : ''}。最终以“需求理解”中的逐条交付物为准，不会把多条脚本误当成一条。
                    </div>
                  ) : null}
                  {!producerMessages.length ? (
                    <div style={{ padding: 10, borderRadius: 8, background: '#eef5ff', color: '#244a7c', fontSize: 13, lineHeight: 1.6 }}>
                      例如：“分析并复刻这个爆款的前3秒钩子、节奏和转化结构，但人物、场景与文案必须原创：https://…” 制片助理会先拆解，再与你确认怎么迁移到本项目。
                    </div>
                  ) : null}
                  {producerMessages.map((message, index) => (
                    <div key={`${message.role}-${index}`} style={{ justifySelf: message.role === 'user' ? 'end' : 'start', maxWidth: '92%', padding: '8px 10px', borderRadius: 9, whiteSpace: 'pre-wrap', lineHeight: 1.55, fontSize: 13, background: message.role === 'user' ? '#3276d2' : '#fff', color: message.role === 'user' ? '#fff' : '#253047', border: message.role === 'user' ? 'none' : '1px solid var(--border)' }}>
                      {message.content}
                    </div>
                  ))}
                  {producerBusy ? <div className="muted" style={{ padding: 6 }}>{producerProgress || '制片助理正在整理方案…'}</div> : null}
                </div>
                {!producerMessages.length ? (
                  <div className="content-factory-quick-starts" style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }} aria-label="常用视频需求">
                    {PRODUCER_QUICK_STARTS.map((item) => (
                      <button
                        key={item.label}
                        className="btn secondary"
                        type="button"
                        onClick={() => applyProducerQuickStart(item.prompt)}
                        disabled={producerBusy}
                        style={{ width: 'auto', minHeight: 34, padding: '6px 10px', fontSize: 12 }}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                ) : null}
                {(producerIntentSpec || producerProposal || producerAuthoritativeScript) ? (
                  <details
                    className="content-factory-brief"
                    open={producerBriefOpen}
                    onToggle={(event) => setProducerBriefOpen(event.currentTarget.open)}
                  >
                    <summary>
                      <span>{producerProposal ? '需求确认与执行方案' : '需求理解'}</span>
                      <span>{producerProposal ? `${producerProposal.video_count} 条 · 待确认` : '查看详情'}</span>
                    </summary>
                    <div className="content-factory-brief__body">
                {producerIntentSpec ? (
                  <div style={{ padding: 12, border: '1px solid #78aaf0', borderRadius: 10, background: '#f7faff', display: 'grid', gap: 9, fontSize: 13 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                      <strong>需求理解</strong>
                      <span style={{ padding: '3px 7px', borderRadius: 999, background: '#e7f0ff', color: '#285d9f', fontSize: 11 }}>
                        {producerDeliveryModeLabel(producerIntentSpec.delivery_mode)}
                      </span>
                    </div>
                    <div>{producerIntentSpec.user_goal}</div>
                    {producerIntentSpec.intent_manifest ? (
                      <div style={{ display: 'grid', gap: 8 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' }}>
                          <strong style={{ fontSize: 12 }}>可追踪创意要求</strong>
                          <span className="muted" style={{ fontSize: 11 }}>
                            v{producerIntentSpec.intent_manifest.schema_version} · {producerIntentSpec.intent_manifest.requirements?.length || 0} 项
                          </span>
                        </div>
                        <div>{producerIntentSpec.intent_manifest.objective}</div>
                        {(producerIntentSpec.intent_manifest.requirements || []).map((item) => (
                          <details key={item.requirement_id} open={item.priority === 'critical'} style={{ border: '1px solid #d6e3f6', borderRadius: 8, background: '#fff', padding: '8px 9px' }}>
                            <summary style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 7, fontWeight: 600 }}>
                              <span style={{ color: '#285d9f' }}>{item.requirement_id}</span>
                              <span style={{ flex: 1 }}>{item.intent}</span>
                              <span style={{ padding: '2px 6px', borderRadius: 999, fontSize: 10, background: item.priority === 'critical' ? '#ffe9e7' : item.priority === 'high' ? '#fff3d9' : '#edf3fb', color: item.priority === 'critical' ? '#a63128' : '#5b6370' }}>
                                {{ critical: '关键', high: '重要', normal: '偏好' }[item.priority] || item.priority}
                              </span>
                            </summary>
                            <div style={{ marginTop: 8, display: 'grid', gap: 6, lineHeight: 1.55 }}>
                              <div><span className="muted">用户原话：</span>“{item.evidence_quote}”</div>
                              <div><span className="muted">执行理解：</span>{item.interpretation}</div>
                              <div>
                                <span className="muted">成片证据：</span>
                                <ul style={{ margin: '4px 0 0', paddingLeft: 20 }}>
                                  {(item.observable_checks || []).map((check) => <li key={check}>{check}</li>)}
                                </ul>
                              </div>
                              {item.creative_freedom?.length ? <div><span className="muted">编导可自由创作：</span>{item.creative_freedom.join('；')}</div> : null}
                              {item.must_not_reuse?.length ? <div><span className="muted">必须重新发明：</span>{item.must_not_reuse.join('；')}</div> : null}
                              {item.deliverable_ordinals?.length ? <div><span className="muted">适用视频：</span>{item.deliverable_ordinals.join('、')}</div> : null}
                              {item.scope === 'time_window' ? <div><span className="muted">时间窗口：</span>{item.start_seconds}-{item.end_seconds} 秒</div> : null}
                            </div>
                          </details>
                        ))}
                      </div>
                    ) : null}
                    {producerIntentSpec.deliverables?.length ? (
                      <div style={{ display: 'grid', gap: 7 }}>
                        {producerIntentSpec.deliverables.map((item) => (
                          <details key={item.ordinal} style={{ border: '1px solid #dbe7f7', borderRadius: 8, background: '#fff', padding: '7px 9px' }}>
                            <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
                              {item.ordinal}. {item.label}{item.target_duration_seconds ? ` · ${item.target_duration_seconds}秒` : ''}
                            </summary>
                            <div style={{ marginTop: 7, display: 'grid', gap: 6, lineHeight: 1.55 }}>
                              <div>{item.objective}</div>
                              {item.differentiation?.length ? <div><span className="muted">本条差异：</span>{item.differentiation.join('；')}</div> : null}
                              {item.script_text ? <pre style={{ margin: 0, padding: 9, maxHeight: 260, overflow: 'auto', whiteSpace: 'pre-wrap', fontFamily: 'inherit', background: '#f8fafc', borderRadius: 7 }}>{item.script_text}</pre> : <div className="muted">文案由编导按本条目标创作。</div>}
                            </div>
                          </details>
                        ))}
                      </div>
                    ) : null}
                    <div className="muted" style={{ fontSize: 11 }}>
                      每项关键要求都会随编号传给编导、分镜、片段执行与最终成片验收；不能只用“快节奏”等标签替代。
                    </div>
                  </div>
                ) : null}
                {producerAuthoritativeScript && !producerIntentSpec?.deliverables?.some((item) => item.script_text) ? (
                  <details style={{ padding: 10, border: '1px solid var(--border)', borderRadius: 8, background: '#fff', fontSize: 13 }}>
                    <summary style={{ cursor: 'pointer', fontWeight: 600 }}>查看当前完整文案{producerAuthoritativeScriptVersion ? ` v${producerAuthoritativeScriptVersion}` : ''}</summary>
                    <pre style={{ margin: '8px 0 0', maxHeight: 320, overflow: 'auto', whiteSpace: 'pre-wrap', fontFamily: 'inherit', lineHeight: 1.6 }}>{producerAuthoritativeScript}</pre>
                  </details>
                ) : null}
                {producerProposal ? (
                  <div style={{ padding: 10, border: '1px solid #9bc2f4', borderRadius: 8, background: '#f5f9ff', display: 'grid', gap: 6, fontSize: 13 }}>
                    <strong>执行参数摘要</strong>
                    <div><span className="muted">目标：</span>{producerProposal.content_objective}</div>
                    <div><span className="muted">规格：</span>{producerProposal.video_count} 条 · {producerProposal.video_duration_min_seconds}-{producerProposal.video_duration_max_seconds} 秒 · {producerProposal.video_aspect_ratio}</div>
                    <div><span className="muted">模型：</span>{videoModelRecord(videoModels, producerProposal.video_model)?.label || producerProposal.video_model}</div>
                    <div><span className="muted">商品角色：</span>{{ required: '成片中使用并承担转化', context_only: '仅用于品类 / 受众背景，不出镜不转化', none: '不使用商品' }[producerProposal.product_use_mode] || (producerProposal.content_mode === 'product' ? '成片中使用并承担转化' : '不使用商品')}</div>
                    <div><span className="muted">时长策略：</span>{producerProposal.video_duration_strategy === 'cross_provider_portable' ? '跨供应商兼容优先' : '创意节奏优先'}</div>
                    <div><span className="muted">口播密度：</span>{{ sparse: '留白型', balanced: '均衡型', dense: '快节奏密集型' }[producerProposal.spoken_density] || producerProposal.spoken_density}{producerProposal.spoken_density_reason ? ` · ${producerProposal.spoken_density_reason}` : ''}</div>
                    <div><span className="muted">生成：</span>{{ text_to_video: '文生视频（零参考图）', image_to_video: '图生视频', video_to_video: '视频生视频' }[producerProposal.video_generation_mode] || '图生视频'}</div>
                    <div><span className="muted">风格：</span>{producerProposal.visual_style}</div>
                    <div><span className="muted">节奏：</span>{producerProposal.pacing}</div>
                    <div><span className="muted">声音：</span>{producerProposal.audio_direction}</div>
                    {producerProposal.conversion_direction ? <div><span className="muted">转化：</span>{producerProposal.conversion_direction}</div> : null}
                    <div className="muted">这是执行边界，不是固定母版。故事、钩子、画面和分段由编导按上方需求动态设计；商品事实仍以公司商品库为准。</div>
                  </div>
                ) : null}
                    </div>
                  </details>
                ) : null}
                {producerStatus === 'created' ? (
                  <div className="content-factory-created-note" style={{ padding: '9px 10px', borderRadius: 8, background: '#eef8f1', color: '#216e3a', fontSize: 12, lineHeight: 1.55 }}>
                    本轮项目已经创建，原项目和成片会完整保留。继续说明“再增加几条”“修改哪些要求”或“做一个新版本”，制片助理会继承上下文并整理新的确认方案。
                  </div>
                ) : null}
                <div className="content-factory-composer">
                  {producerAttachmentAnalysisPending ? <div className="content-factory-composer__notice">素材正在分析，可以继续编辑需求；分析完成后即可发送。</div> : null}
                  <form onSubmit={sendProducerMessage}>
                    <textarea
                      ref={producerComposerRef}
                      className="input"
                      rows={3}
                      maxLength={50000}
                      placeholder={producerStatus === 'created' ? '继续提出追加或修改要求，也可以发一个爆款视频链接作为新对标……' : '说说你想做什么视频，或直接粘贴完整文案、爆款视频链接……'}
                      value={producerInput}
                      onChange={(event) => setProducerInput(event.target.value)}
                      onKeyDown={handleProducerComposerKeyDown}
                      aria-label="给 AI 制片助理发送消息"
                    />
                    <div className="content-factory-composer__footer">
                      <span>Enter 发送 · Shift+Enter 换行</span>
                      <button className="btn" disabled={producerBusy || producerUploadBusy || producerAttachmentAnalysisPending || !producerInput.trim()}>
                        {producerBusy ? '助理思考中…' : producerStatus === 'created' ? '发送新要求' : producerMessages.length ? '发送' : '开始沟通'}
                      </button>
                    </div>
                  </form>
                  {producerStatus === 'proposal_ready' && producerProposalSha ? (
                    <button className="btn content-factory-confirm-button" type="button" onClick={() => confirmProducerProject()} disabled={producerBusy || !producerPendingDecisionId}>确认方案并创建 {producerProposal?.video_count || ''} 条视频任务</button>
                  ) : null}
                  <button className="content-factory-text-button" type="button" onClick={resetProducerConversation} disabled={producerBusy || producerUploadBusy}>开始新的需求</button>
                </div>
            </section>
          </aside>
          <main className="card content-factory-production-panel">
            {selected ? <header className="content-factory-production-header">
              <div className="content-factory-production-header__identity">
                <div>
                  <h3>{selected.title}</h3>
                  <p>{selected.product_name || '非商品内容'} · {selected.market}</p>
                </div>
                <span style={statusTone(selected.status)}>{projectStatusText(selected)}</span>
              </div>
              <div className="content-factory-production-header__actions">
                {selected.config_json?.producer_intake?.session_key ? (
                  <button className="btn" type="button" onClick={() => continueProducerConversationForProject(selected)} disabled={busy || producerBusy || producerUploadBusy}>
                    继续沟通
                  </button>
                ) : null}
                <button className="btn secondary" type="button" onClick={refresh}>刷新</button>
                <details className="content-factory-project-menu">
                  <summary className="btn secondary">更多</summary>
                  <div>
                    <button type="button" onClick={() => setProjectEditOpen((value) => !value)} disabled={busy}>编辑项目</button>
                    {selected.config_json?.allow_reference_video ? <label>上传对标视频<input type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm" onChange={(event) => uploadFiles(event, 'reference_video')} disabled={busy} hidden /></label> : null}
                    <button type="button" onClick={() => setCharacterUploadOpen((value) => !value)} disabled={busy}>管理人物锚点</button>
                {selected.status === 'paused' ? (
                      <button type="button" onClick={resumeProject} disabled={busy}>恢复项目</button>
                ) : (
                      <button type="button" onClick={pauseProject} disabled={busy || ['complete'].includes(selected.status)}>暂停项目</button>
                )}
                    <button type="button" onClick={deleteProject} disabled={busy} className="is-danger">删除项目</button>
                  </div>
                </details>
              </div>
              <details className="content-factory-project-specs">
                <summary>查看制作规格</summary>
                <div>
                  <span>Bridge {selected.browser_slot ?? '未分配'}</span>
                  <span>目标时长 {selected.config_json?.video_duration_min_seconds || 10}-{selected.config_json?.video_duration_max_seconds || 10} 秒</span>
                  <span>{videoSegmentLabel(videoModels, selected.config_json?.video_model)}</span>
                  <span>{selected.config_json?.video_resolution || '720p'}</span>
                  <span>{selected.config_json?.video_aspect_ratio || selected.config_json?.director_series_brief?.aspect_ratio || '9:16'}</span>
                  <span>{selected.config_json?.video_language === 'zh-CN' ? '简体中文' : 'English (US)'}</span>
                </div>
              </details>
            </header> : null}
            {selected && characterUploadOpen ? (
              <section style={{ marginBottom: 12, padding: 12, border: '1px solid #9bbce7', borderRadius: 6, background: '#f8fbff', display: 'grid', gap: 10 }}>
                <div>
                  <strong>人物锚点</strong>
                  <div className="muted" style={{ marginTop: 3, fontSize: 12 }}>同一人物的多张图片会作为一组同时发送给 ChatGPT；不同人物不会混组。</div>
                </div>
                {selectedCharacterGroups.length ? (
                  <div style={{ display: 'grid', gap: 6 }}>
                    {selectedCharacterGroups.map((group) => (
                      <div key={group.key} style={{ padding: 8, border: '1px solid var(--border)', borderRadius: 6, background: '#fff' }}>
                        <strong>{group.name}</strong>
                        <span className="muted" style={{ marginLeft: 8 }}>{group.files.length} 张图片</span>
                        {group.description ? <div className="muted" style={{ marginTop: 4 }}>{group.description}</div> : null}
                      </div>
                    ))}
                  </div>
                ) : <span className="muted">当前项目尚未添加人物锚点。</span>}
                <div style={{ display: 'grid', gridTemplateColumns: 'minmax(160px, 240px) minmax(260px, 1fr)', gap: 8 }}>
                  <input className="input" value={projectCharacterDraft.name} maxLength={120} placeholder="人物名称" onChange={(event) => setProjectCharacterDraft({ ...projectCharacterDraft, name: event.target.value })} />
                  <textarea className="input" rows={2} maxLength={2000} value={projectCharacterDraft.description} placeholder="人物描述：年龄范围、脸型、发型、身材、服装、角色关系等" onChange={(event) => setProjectCharacterDraft({ ...projectCharacterDraft, description: event.target.value })} />
                </div>
                <label style={{ display: 'grid', gap: 4, padding: 10, border: '1px dashed #9bbce7', borderRadius: 6, cursor: 'pointer', background: '#fff' }}>
                  <strong>选择该人物的图片（可多张）</strong>
                  <input type="file" multiple accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" onChange={(event) => setProjectCharacterDraft({ ...projectCharacterDraft, files: Array.from(event.target.files || []) })} disabled={busy} style={{ display: 'none' }} />
                  <span className="muted">{projectCharacterDraft.files.length ? `${projectCharacterDraft.files.length} 张：${projectCharacterDraft.files.map((file) => file.name).join('、')}` : '尚未选择图片'}</span>
                </label>
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <button className="btn" type="button" onClick={uploadProjectCharacter} disabled={busy || !projectCharacterDraft.files.length}>上传该人物</button>
                </div>
              </section>
            ) : null}
            {!selected ? <div className="content-factory-production-empty"><strong>还没有项目</strong><span>先在左侧告诉制片助理你想制作什么视频。</span><button className="btn" type="button" onClick={focusProducerComposer}>开始沟通</button></div> : <>
              {projectEditOpen && projectEditForm ? (
                <section style={{ marginTop: 12, padding: 12, border: '1px solid #9bbce7', borderRadius: 6, background: '#f8fbff' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center', marginBottom: 10 }}>
                    <strong>编辑已保存的项目状态</strong>
                    <span className="muted" style={{ fontSize: 12 }}>设置保存在服务器，刷新或重新登录不会丢失</span>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8 }}>
                    <input className="input" aria-label="项目名称" value={projectEditForm.title} onChange={(e) => setProjectEditForm({ ...projectEditForm, title: e.target.value })} style={{ gridColumn: 'span 2' }} />
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>裂变视频数量</span><input className="input" disabled={mediaContractLocked} type="number" min="1" max="50" value={projectEditForm.video_count} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_count: Number(e.target.value) || 1 })} /></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>同时生成视频数</span><select className="input" value={projectEditForm.max_api_video_variants_in_flight} onChange={(e) => setProjectEditForm({ ...projectEditForm, max_api_video_variants_in_flight: Number(e.target.value) })}>{[1, 2, 3, 4].map((count) => <option key={count} value={count}>{count} 条</option>)}</select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>视频模型</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_model} onChange={(e) => { const model = e.target.value; setProjectEditForm({ ...projectEditForm, video_model: model, video_reference_limit: Math.min(projectEditForm.video_reference_limit, videoReferenceLimit(videoModels, model)) }) }}>{videoModels.filter((item) => item.available || item.id === projectEditForm.video_model).map((item) => <option key={item.id} value={item.id}>{item.label}{item.available ? '' : '（当前不可用）'}</option>)}</select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>时长规划策略</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_duration_strategy} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_duration_strategy: e.target.value })}><option value="creative_flexibility">创意节奏优先</option><option value="cross_provider_portable">跨供应商兼容优先</option></select></label>
                    <textarea className="input" disabled={mediaContractLocked} rows={2} maxLength={255} aria-label="内容目标" placeholder="这批视频要达成什么目标？" value={projectEditForm.content_objective || ''} onChange={(e) => setProjectEditForm({ ...projectEditForm, content_objective: e.target.value })} style={{ gridColumn: '1 / -1' }} />
                    <textarea className="input" disabled={mediaContractLocked} rows={2} maxLength={1000} aria-label="目标受众" placeholder="目标受众、处境、认知和购买顾虑" value={projectEditForm.target_audience || ''} onChange={(e) => setProjectEditForm({ ...projectEditForm, target_audience: e.target.value })} style={{ gridColumn: '1 / -1' }} />
                    <textarea className="input" disabled={mediaContractLocked} rows={4} aria-label="事实与限制" placeholder="补充真实资料和必须遵守的限制；不要写固定段数或场景母版。" value={projectEditForm.product_brief} onChange={(e) => setProjectEditForm({ ...projectEditForm, product_brief: e.target.value })} style={{ gridColumn: '1 / -1' }} />
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>最短时长（秒）</span><input className="input" disabled={mediaContractLocked} type="number" min="1" max="120" value={projectEditForm.video_duration_min_seconds} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_duration_min_seconds: Number(e.target.value) || 1 })} /></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>最长时长（秒）</span><input className="input" disabled={mediaContractLocked} type="number" min="1" max="120" value={projectEditForm.video_duration_max_seconds} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_duration_max_seconds: Number(e.target.value) || 1 })} /></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>分辨率</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_resolution} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_resolution: e.target.value })}><option value="720p">720p</option><option value="480p">480p</option></select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>画面比例</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_aspect_ratio} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_aspect_ratio: e.target.value })}><option value="9:16">9:16 竖屏</option><option value="16:9">16:9 横屏</option><option value="1:1">1:1 方形</option></select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>语言</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_language} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_language: e.target.value })}><option value="en-US">English (US)</option><option value="zh-CN">简体中文</option></select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>单段参考图上限</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_reference_limit} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_reference_limit: Number(e.target.value) })}>{Array.from({ length: videoReferenceLimit(videoModels, projectEditForm.video_model) }, (_, index) => index + 1).map((count) => <option key={count} value={count}>{count} 张</option>)}</select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>参考帧模式</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_frame_mode} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_frame_mode: e.target.value })}><option value="reference">多参考图</option><option value="first_last">首尾帧</option></select></label>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12 }}><input type="checkbox" disabled={mediaContractLocked} checked={projectEditForm.allow_reference_video} onChange={(e) => setProjectEditForm({ ...projectEditForm, allow_reference_video: e.target.checked })} />Hermes 分析对标视频</label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>视频生成方式</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_generation_mode} onChange={(e) => { const mode = e.target.value; setProjectEditForm({ ...projectEditForm, video_generation_mode: mode, allow_reference_video: mode === 'video_to_video' ? true : false }) }}><option value="text_to_video">文生视频（零参考图）</option><option value="image_to_video">图生视频</option><option value="video_to_video">视频生视频</option></select></label>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12 }}><input type="checkbox" checked={projectEditForm.auto_run} onChange={(e) => setProjectEditForm({ ...projectEditForm, auto_run: e.target.checked })} />保存后无人值守执行</label>
                    {mediaContractLocked ? <div className="muted" style={{ gridColumn: '1 / -1' }}>项目已进入媒体生产，数量、内容目标、事实、文案、模型、比例、分辨率、语言、时长和参考帧契约已锁定；仍可调整并行数和自动运行。如需改变生产契约，请新建项目。</div> : null}
                  </div>
                  <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', alignItems: 'end', marginTop: 12, flexWrap: 'wrap' }}>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>重新开始阶段</span><select className="input" value={restartStage} onChange={(e) => setRestartStage(e.target.value)}>{RESTART_STAGES.map((stage) => <option key={stage} value={stage}>{LABELS[stage]}</option>)}</select></label>
                    <button className="btn secondary" type="button" onClick={() => saveProjectSettings(false)} disabled={busy}>仅保存设置</button>
                    <button className="btn" type="button" onClick={() => saveProjectSettings(true)} disabled={busy || ['queued', 'running', 'generating_video'].includes(selected.status)}>保存并重新开始</button>
                  </div>
                </section>
              ) : null}
              <div style={{ display: 'flex', gap: 6, overflowX: 'auto', padding: '14px 0 10px' }}>
                {STAGES.map((stage) => {
                  const active = stage === selected.current_stage
                  const passed = STAGES.indexOf(stage) >= 0 && STAGES.indexOf(stage) < STAGES.indexOf(selected.current_stage)
                  return <div key={stage} style={{ flex: '0 0 auto', padding: '7px 9px', borderRadius: 6, border: active ? '1px solid #3b82f6' : '1px solid var(--border)', background: active ? '#eaf3ff' : passed ? '#eef9f2' : '#fff', fontSize: 12, fontWeight: active ? 700 : 500 }}>{LABELS[stage]}</div>
                })}
              </div>
              {recoveryDecision ? (
                <details style={{ margin: '0 0 12px', padding: 12, border: '1px solid #9bc2f4', borderRadius: 8, background: '#f8fbff' }}>
                  <summary style={{ cursor: 'pointer', fontWeight: 700 }}>
                    故障调度官 · {recoveryActionText(recoveryDecision.action)}
                  </summary>
                  <div style={{ display: 'grid', gap: 6, marginTop: 9, fontSize: 12 }}>
                    <div><strong>诊断：</strong>{recoveryDecision.diagnosis || recoveryDecision.rationale || '已依据当前故障包完成判断。'}</div>
                    {recoveryDecision.repair_directive ? <div><strong>修复指令：</strong>{recoveryDecision.repair_directive}</div> : null}
                    <div className="muted">来源：{recoveryDecision.decision_source === 'model' ? '多模态模型' : '安全兜底'} · 置信度 {Math.round(Number(recoveryDecision.confidence || 0) * 100)}% · 第 {Number(recoveryDecision.recovery_cycle || 0)} 次恢复</div>
                    {Array.isArray(recoveryDecision.evidence_used) && recoveryDecision.evidence_used.length ? <div><strong>使用证据：</strong>{recoveryDecision.evidence_used.join('；')}</div> : null}
                  </div>
                </details>
              ) : null}
              {paused ? <div style={{ margin: '0 0 12px', padding: 12, border: '1px solid #f0c36d', borderRadius: 6, background: '#fffaf0', display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}><div><strong>已暂停，可人工干预</strong><div className="muted" style={{ marginTop: 4 }}>{selected.last_error || '检查编导、API、额度或素材后，点恢复项目从断点继续；浏览器只在兜底时需要在线。'}</div></div><button className="btn" type="button" onClick={resumeProject} disabled={busy}>恢复执行</button></div> : null}
              {selectedIntentManifest ? (
                <details style={{ margin: '0 0 12px', padding: 12, border: '1px solid #9bc2f4', borderRadius: 8, background: '#f8fbff' }}>
                  <summary style={{ cursor: 'pointer', fontWeight: 700 }}>创意要求执行链 · {selectedIntentManifest.requirements?.length || 0} 项</summary>
                  <div className="muted" style={{ marginTop: 7, fontSize: 12 }}>
                    客服意图 → 编导映射 {directorApproved ? '✓' : '…'} → 制作方案映射 {productionPlanApproved ? '✓' : '…'} → 成片证据 {Object.keys(finalIntentEvidence).length ? '✓' : '…'}
                  </div>
                  <div style={{ display: 'grid', gap: 7, marginTop: 9 }}>
                    {(selectedIntentManifest.requirements || []).map((item) => {
                      const evidence = finalIntentEvidence[item.requirement_id] || null
                      const status = String(evidence?.status || '').toLowerCase()
                      return (
                        <div key={item.requirement_id} style={{ padding: 8, borderRadius: 7, background: '#fff', border: '1px solid #dbe7f7', display: 'grid', gap: 4 }}>
                          <div style={{ display: 'flex', gap: 7, alignItems: 'center' }}>
                            <strong style={{ color: '#285d9f' }}>{item.requirement_id}</strong>
                            <span style={{ flex: 1 }}>{item.intent}</span>
                            <span style={{ fontSize: 11, color: status === 'pass' ? '#21713b' : status === 'fail' ? '#b52d25' : '#6b7280' }}>
                              {status === 'pass' ? '成片通过' : status === 'fail' ? '成片未通过' : '执行中'}
                            </span>
                          </div>
                          <div className="muted" style={{ fontSize: 11 }}>{item.interpretation}</div>
                          {evidence?.observed_evidence?.length ? <div style={{ fontSize: 12 }}><span className="muted">已观察到：</span>{evidence.observed_evidence.join('；')}</div> : null}
                          {evidence?.missing_checks?.length ? <div style={{ fontSize: 12, color: '#b52d25' }}><span>缺失：</span>{evidence.missing_checks.join('；')}</div> : null}
                        </div>
                      )
                    })}
                  </div>
                </details>
              ) : null}
              {(selected.status === 'generating_video' || videoGroups.length > 0 || projectComposedVideoCount > 0) ? (
                <div style={{ margin: '0 0 12px', padding: 12, border: '1px solid var(--border)', borderRadius: 6, background: '#f8fbff' }}>
                  <strong>视频生成进度</strong>
                  <div className="muted" style={{ marginTop: 4 }}>
                    已合成 {projectComposedVideoCount} 个完整视频
                    {pendingVideoTaskCount ? `，${pendingVideoTaskCount} 个片段仍在生成/下载` : ''}
                    {failedVideoTaskCount ? `，${failedVideoTaskCount} 个片段失败，系统会按片段重试或补齐目标数` : ''}
                  </div>
                  {videoGroups.length ? (
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
                      {videoGroups.map((group) => (
                        <span key={`${group.video_index}-${group.status}`} style={{ ...statusTone(group.status === 'composed' ? 'success' : group.status), padding: '3px 7px', borderRadius: 10, fontSize: 12 }}>
                          V{group.video_index}: {group.status === 'composed' ? '已合成' : group.status === 'failed' ? '失败' : '等待中'}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(180px,240px) auto', gap: 10, alignItems: 'end', borderTop: '1px solid var(--border)', paddingTop: 12 }}>
                <div><label style={{ display: 'block', fontWeight: 600, marginBottom: 6 }}>给 Hermes 的补充指令</label><textarea className="input" rows={3} value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="例如：本轮优先 post-Pilates 场景，不使用任何促销。" /></div>
                <label style={{ display: 'grid', gap: 6, fontWeight: 600 }}>
                  <span>执行阶段</span>
                  <select className="input" value={targetStage || selected.current_stage} onChange={(e) => setTargetStage(e.target.value)}>
                    {EXECUTION_STAGES.map((stage) => <option key={stage} value={stage}>{LABELS[stage] || stage}</option>)}
                  </select>
                </label>
                <div style={{ display: 'grid', gap: 8 }}>
                  <button className="btn" type="button" onClick={() => runStage('continue')} disabled={!canRun}>从此自动执行</button>
                  <button className="btn secondary" type="button" onClick={() => runStage('single')} disabled={!canRun}>只执行此阶段</button>
                </div>
              </div>
              <label style={{ display: 'block', marginTop: 12, border: '1px dashed #9bbce7', borderRadius: 6, padding: 14, cursor: 'pointer', background: '#f7fbff' }}>
                <strong>上传项目补充参考图</strong><span className="muted" style={{ marginLeft: 8 }}>JPG / PNG，最多 20 个</span>
                <input type="file" multiple accept="image/*" onChange={uploadFiles} disabled={busy} style={{ display: 'none' }} />
              </label>
              <section style={{ marginTop: 16, padding: 14, border: '1px solid var(--border)', borderRadius: 8, background: '#fff' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                  <div>
                    <h4 style={{ margin: 0 }}>成果交付</h4>
                    <div className="muted" style={{ marginTop: 4 }}>
                      已完成 {deliverableCompleteCount}/{deliverableTargetCount || deliverableItems.length || 0} 条视频
                      {deliverables.guidance_count ? ` · 剪辑指导 ${deliverables.guidance_count} 份` : ''}
                      {Array.isArray(deliverables.missing_indices) && deliverables.missing_indices.length ? ` · 待补 V${deliverables.missing_indices.join('、V')}` : ''}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    {hasDeliverableFiles ? (
                      <>
                        <a className="btn" href={contentFactoryDeliverablesZipUrl(wid, selected.project_key, 'all')}>批量下载全部成果</a>
                        <a className="btn secondary" href={contentFactoryDeliverablesZipUrl(wid, selected.project_key, 'videos')}>只下载视频</a>
                        <a className="btn secondary" href={contentFactoryDeliverablesZipUrl(wid, selected.project_key, 'guides')}>只下载指导</a>
                      </>
                    ) : (
                      <button className="btn secondary" type="button" disabled>等待成果生成</button>
                    )}
                  </div>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 12, marginTop: 12 }}>
                  {(deliverableItems.length ? deliverableItems : Array.from({ length: Math.max(1, deliverableTargetCount || 1) }, (_, index) => ({ index: index + 1, status: 'missing' }))).map((item) => {
                    const videoUrl = item.video ? contentFactoryAssetUrl(wid, selected.project_key, item.video.id) : ''
                    const guideUrl = item.guidance ? contentFactoryAssetUrl(wid, selected.project_key, item.guidance.id) : ''
                    const tone = deliverableStatusTone(item.status)
                    return (
                      <article key={`deliverable-${item.index}`} style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden', background: '#fbfcff', minWidth: 0 }}>
                        <div style={{ padding: 10, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                          <strong>V{String(item.index).padStart(2, '0')}</strong>
                          <span style={{ ...tone, padding: '3px 8px', borderRadius: 999, fontSize: 12, fontWeight: 700 }}>{deliverableStatusLabel(item.status)}</span>
                        </div>
                        {item.video ? (
                          <video src={videoUrl} controls preload="metadata" style={{ width: '100%', height: 'auto', maxHeight: 360, background: '#111', display: 'block' }} />
                        ) : (
                          <div style={{ height: 180, display: 'grid', placeItems: 'center', color: '#6b7280', background: '#f4f6f9', borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)' }}>
                            {item.status === 'guide_only' ? '缺少视频文件' : '等待视频生成'}
                          </div>
                        )}
                        <div style={{ padding: 10, display: 'grid', gap: 8 }}>
                          {item.video ? (
                            <div title={item.video.original_name} style={{ fontSize: 12, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {item.video.original_name} {formatFileSize(item.video.size_bytes) ? `· ${formatFileSize(item.video.size_bytes)}` : ''}
                            </div>
                          ) : null}
                          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                            {item.video ? <a className="btn secondary" href={videoUrl} target="_blank" rel="noreferrer">下载视频</a> : null}
                            {item.guidance ? <a className="btn secondary" href={guideUrl} target="_blank" rel="noreferrer">剪辑指导</a> : <span className="muted" style={{ alignSelf: 'center', fontSize: 12 }}>指导生成中</span>}
                          </div>
                        </div>
                      </article>
                    )
                  })}
                </div>
              </section>
              <details style={{ marginTop: 16, border: '1px solid var(--border)', borderRadius: 8, padding: 12, background: '#fff' }}>
                <summary style={{ cursor: 'pointer', fontWeight: 700 }}>高级：项目资产与中间文件 ({selected.assets?.length || 0})</summary>
                <div className="muted" style={{ marginTop: 8 }}>这里保留给排查使用，正式交付请以上面的“成果交付”为准。</div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10, marginTop: 10 }}>
                  {selected.assets?.map((asset) => {
                    const url = contentFactoryAssetUrl(wid, selected.project_key, asset.id)
                    const isImage = asset.mime_type?.startsWith('image/')
                    const isVideo = asset.mime_type?.startsWith('video/')
                    return <div key={asset.id} style={{ border: '1px solid var(--border)', borderRadius: 6, overflow: 'hidden', background: '#fff' }}>
                      {isImage ? <img src={url} alt={asset.original_name} style={{ width: '100%', height: 'auto', maxHeight: 360, objectFit: 'contain', display: 'block', background: '#f6f7f9' }} /> : null}
                      {isVideo ? <video src={url} controls preload="metadata" style={{ width: '100%', height: 'auto', maxHeight: 260, background: '#111', display: 'block' }} /> : null}
                      <div style={{ padding: 8, minWidth: 0 }}>
                        <div title={asset.original_name} style={{ fontSize: 12, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{asset.original_name}</div>
                        <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>{LABELS[asset.stage] || asset.stage || asset.kind}</div>
                        {!isImage && !isVideo ? <a href={url} target="_blank" rel="noreferrer" style={{ display: 'inline-block', marginTop: 6 }}>查看文件</a> : null}
                      </div>
                    </div>
                  })}
                </div>
              </details>
              <details style={{ marginTop: 16, border: '1px solid var(--border)', borderRadius: 8, padding: 12, background: '#fff' }}>
                <summary style={{ cursor: 'pointer', fontWeight: 700 }}>高级：阶段记录 ({selected.stages?.length || 0})</summary>
                <div style={{ display: 'grid', gap: 8, marginTop: 10 }}>{[...(selected.stages || [])].reverse().map((stage) => { const displayStatus = stageDisplayStatus(stage); return <details key={stage.id} style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 10 }}><summary style={{ cursor: 'pointer', display: 'flex', gap: 10, alignItems: 'center' }}><strong>{LABELS[stage.stage] || stage.stage}</strong><span style={{ ...statusTone(displayStatus), padding: '3px 7px', borderRadius: 10, fontSize: 12 }}>{stageStatusText(stage)}</span><span className="muted">尝试 {stage.attempt}</span></summary>{stage.error_message ? <div className={displayStatus === 'superseded' ? 'alert' : 'alert error'} style={{ marginTop: 10 }}>{stage.error_message}</div> : null}<pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 420, overflow: 'auto', background: 'var(--panel-2)', padding: 10 }}>{stage.response_text || JSON.stringify(stage.output_json, null, 2) || '等待结果'}</pre>{stage.chat_url ? <a href={stage.chat_url} target="_blank" rel="noreferrer">打开 GPT 会话</a> : null}</details> })}</div>
              </details>
            </>}
          </main>
        </div>
      )}
    </div>
  )
}
