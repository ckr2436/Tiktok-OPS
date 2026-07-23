import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useSessionQuery } from '@/features/platform/auth/hooks.js'
import {
  bindContentFactoryBridgeDevice,
  contentFactoryAssetUrl,
  contentFactoryDeliverablesZipUrl,
  contentFactoryProductAssetUrl,
  confirmContentFactoryProducerProject,
  createContentFactoryProduct,
  createContentFactoryProject,
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

const STAGES = ['FACTS', 'SERIES_DIRECTOR', 'DIRECTOR', 'PRODUCTION_PLAN', 'VISUAL_PREVIEW', 'CREATIVE_REVIEW', 'FINAL_ASSETS', 'VIDEO_PROMPTS', 'WAITING_VIDEO_INPUT', 'EDIT_PACKAGE', 'COMPLETE']
const EXECUTION_STAGES = ['FACTS', 'SERIES_DIRECTOR', 'DIRECTOR', 'PRODUCTION_PLAN', 'VISUAL_PREVIEW', 'CREATIVE_REVIEW', 'FINAL_ASSETS', 'VIDEO_PROMPTS', 'EDIT_PACKAGE']
const RESTART_STAGES = ['SERIES_DIRECTOR', 'DIRECTOR', 'PRODUCTION_PLAN', 'VISUAL_PREVIEW', 'CREATIVE_REVIEW', 'FINAL_ASSETS', 'VIDEO_PROMPTS', 'EDIT_PACKAGE']
const PRODUCER_RECOVERY_DEADLINE_MS = 210_000
const PRODUCER_RECOVERY_INTERVAL_MS = 2_000
const LABELS = {
  FACTS: '产品事实',
  SERIES_DIRECTOR: '整批编导',
  DIRECTOR: '单条编导',
  PRODUCTION_PLAN: '制作方案',
  CREATIVE: '旧版创意（只读）',
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
  return 'omni_flash'
}

function videoReferenceLimit(model) {
  if (normalizeVideoModel(model) === 'seedance_2_0_mini') return 9
  return 7
}

function videoSegmentLabel(model) {
  if (normalizeVideoModel(model) === 'seedance_2_0_mini') return 'Seedance 2.0 Mini 每片最长 15 秒'
  return 'Omni Flash 每片 10 秒'
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

const EMPTY_PROJECT_FORM = {
  title: '', content_objective: '', target_audience: '',
  content_mode: 'product', product_id: '', product_brief: '', video_count: 10,
  max_api_video_variants_in_flight: 2,
  video_duration_min_seconds: 10, video_duration_max_seconds: 10,
  video_model: 'omni_flash', video_resolution: '720p', video_aspect_ratio: '9:16', video_language: 'en-US',
  video_reference_limit: 7, video_frame_mode: 'reference', allow_reference_video: false,
  confirmed_claims: '', confirmed_selling_points: '', confirmed_promotions: '',
  promotion_cta: '', allow_promotional_cta: true,
  content_director_mode: 'enforce', auto_run: true,
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
  return !error?.status && !error?.response?.status
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
    return '对标视频仍在提取画面和口播文案，请等待附件显示“分析完成”后再继续。'
  }
  if (code === 'HERMES_TIMEOUT' || error?.status === 504) {
    return '内容模型本次响应超时。你的文字和附件都已保存，可以安全重试，不会重复写入消息。'
  }
  if (error?.status >= 500) {
    return 'AI 制片助理服务暂时不可用。你的文字和附件都已保存，请稍后重试。'
  }
  return error?.message || 'AI 制片助理暂时无法响应。'
}

function attachmentKindLabel(kind) {
  return kind === 'reference_video' ? '对标视频' : '人物参考图'
}

function formatAttachmentSize(bytes) {
  const value = Number(bytes || 0)
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`
  return `${Math.max(1, Math.round(value / 1024))} KB`
}

function attachmentAnalysisLabel(attachment) {
  if (['queued', 'processing'].includes(attachment?.analysis_status)) return '分析中'
  const transcriptStatus = String(attachment?.analysis?.transcript_status || '')
  if (transcriptStatus === 'failed') return '画面完成 · 口播提取失败'
  if (transcriptStatus === 'no_speech') return '分析完成 · 无可识别口播'
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
    product_brief: project?.product_brief || '',
    video_count: Number(config.video_count || 10),
    max_api_video_variants_in_flight: Number(config.max_api_video_variants_in_flight || 1),
    video_duration_min_seconds: Number(config.video_duration_min_seconds || config.video_duration_seconds || 10),
    video_duration_max_seconds: Number(config.video_duration_max_seconds || config.video_duration_seconds || 10),
    video_model: normalizeVideoModel(config.video_model),
    video_resolution: config.video_resolution || '720p',
    video_aspect_ratio: config.video_aspect_ratio || config.director_series_brief?.aspect_ratio || '9:16',
    video_language: config.video_language || 'en-US',
    video_reference_limit: Number(config.video_reference_limit || 7),
    video_frame_mode: config.video_frame_mode || 'reference',
    allow_reference_video: Boolean(config.allow_reference_video),
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
  const sessionQuery = useSessionQuery()
  const currentUserId = sessionQuery.data?.id
  const draftStorageKey = useMemo(
    () => contentFactoryUserStorageKey('draft', wid, currentUserId),
    [wid, currentUserId],
  )
  const producerStorageKey = useMemo(
    () => contentFactoryUserStorageKey('producer', wid, currentUserId),
    [wid, currentUserId],
  )
  const [tab, setTab] = useState('factory')
  const [projects, setProjects] = useState([])
  const [products, setProducts] = useState([])
  const [selectedKey, setSelectedKey] = useState('')
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
  const [form, setForm] = useState(EMPTY_PROJECT_FORM)
  const [pendingBenchmarkVideoFiles, setPendingBenchmarkVideoFiles] = useState([])
  const [pendingCharacters, setPendingCharacters] = useState([])
  const [characterUploadOpen, setCharacterUploadOpen] = useState(false)
  const [projectCharacterDraft, setProjectCharacterDraft] = useState(() => newCharacterDraft(1))
  const [projectEditForm, setProjectEditForm] = useState(null)
  const [restartStage, setRestartStage] = useState('DIRECTOR')
  const [projectEditOpen, setProjectEditOpen] = useState(false)
  const [creationMode, setCreationMode] = useState('assistant')
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
  const [producerBusy, setProducerBusy] = useState(false)
  const [producerProgress, setProducerProgress] = useState('')
  const [producerSessionLoadError, setProducerSessionLoadError] = useState('')
  const [producerSessionReloadNonce, setProducerSessionReloadNonce] = useState(0)
  const producerAttachmentAnalysisPending = producerAttachments.some(
    (item) => ['queued', 'processing'].includes(item.analysis_status),
  )

  const selected = useMemo(() => projects.find((item) => item.project_key === selectedKey) || projects[0] || null, [projects, selectedKey])
  const selectedProduct = useMemo(() => products.find((item) => String(item.id) === String(form.product_id || selectedProductId)) || null, [products, selectedProductId, form.product_id])
  const selectedCharacterGroups = useMemo(() => characterGroupsFromAssets(selected?.assets || []), [selected?.assets])
  const mediaContractLocked = useMemo(() => projectMediaContractLocked(selected), [selected])
  const selectedBridgeDeviceId = String(bridge?.selected_device_id || '')
  const currentDeviceSlots = useMemo(
    () => (Array.isArray(bridge?.slots) ? bridge.slots : []).filter(
      (slot) => String(slot?.agent_device_id || '') === selectedBridgeDeviceId,
    ),
    [bridge?.slots, selectedBridgeDeviceId],
  )

  useEffect(() => {
    if (!draftStorageKey) return
    try { window.localStorage.removeItem(`content-factory-draft:${wid}`) } catch (_) { /* remove legacy unscoped draft */ }
    setBridgeForm((prev) => ({
      ...prev,
      device_name: prev.device_name || `${navigator.platform || 'Browser'} · ${navigator.userAgent.includes('Chrome') ? 'Chrome' : 'Browser'}`,
      inbox_root: prev.inbox_root || 'C:\\Users\\sqkj01\\AppData\\Local\\MYUPONA\\HermesInbox',
    }))
    try {
      const saved = window.localStorage.getItem(draftStorageKey)
      if (saved) {
        const draft = { ...EMPTY_PROJECT_FORM, ...JSON.parse(saved) }
        setForm(draft)
        if (draft.product_id) setSelectedProductId(String(draft.product_id))
      }
    } catch (_) { /* Ignore an invalid old browser draft. */ }
  }, [draftStorageKey])

  useEffect(() => {
    if (!producerStorageKey) return undefined
    try { window.localStorage.removeItem(`content-factory-producer:${wid}`) } catch (_) { /* remove legacy unscoped session */ }
    let cancelled = false
    let savedKey = ''
    try { savedKey = window.localStorage.getItem(producerStorageKey) || '' } catch (_) { savedKey = '' }
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
      }
      setProducerProposal(session?.proposal || null)
      setProducerProposalSha(session?.proposal_sha256 || '')
      setProducerStatus(session?.status || 'idle')
      setProducerAuthoritativeScriptId(session?.authoritative_script_message_id || null)
      if (session?.selected_product_id) setProducerProductId(String(session.selected_product_id))
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
  }, [wid, producerStorageKey, producerSessionReloadNonce])

  useEffect(() => {
    if (!wid || !producerSessionKey || !producerAttachmentAnalysisPending) return undefined
    let cancelled = false
    const poll = async () => {
      try {
        const session = await fetchContentFactoryProducerSession(wid, producerSessionKey)
        if (cancelled) return
        const attachments = Array.isArray(session?.attachments) ? session.attachments : []
        setProducerAttachments(attachments)
        if (!attachments.some((item) => ['queued', 'processing'].includes(item.analysis_status))) {
          setNotice(attachments.some((item) => item?.analysis?.transcript_status === 'failed')
            ? '对标视频画面已分析完成，但口播提取失败；制片助理会仅按可见画面理解，不会臆测音频。'
            : '对标视频的关键画面和口播文案已分析完成，制片助理现在可以结合附件理解。')
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
    if (!draftStorageKey) return undefined
    const timer = window.setTimeout(() => {
      window.localStorage.setItem(draftStorageKey, JSON.stringify(form))
    }, 250)
    return () => window.clearTimeout(timer)
  }, [draftStorageKey, form])

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
        setForm((current) => current.content_mode !== 'product' || current.product_id ? current : ({ ...current, product_id: String(productItems[0].id) }))
      }
      setError('')
    } catch (err) {
      setError(err?.message || '内容工厂状态加载失败。')
    }
  }, [wid, selectedKey, selectedProductId])

  useEffect(() => { refresh() }, [refresh])
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

  async function installBridgeAgent(event) {
    event?.preventDefault?.()
    event?.stopPropagation?.()
    setBridgeBusy(true); setBridgeMessage('正在生成当前用户和设备专用的 Windows 浏览器桥...'); setError('')
    try {
      const blob = await downloadContentFactoryBridgeAgent(wid, {
        deviceId: getBridgeDeviceId(wid),
        deviceName: bridgeForm.device_name || navigator.platform || 'Windows device',
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
    if (producerAttachments.length && producerSessionKey && producerStatus !== 'created') {
      setProducerUploadBusy(true); setError('')
      try {
        await Promise.all(producerAttachments.map((attachment) => (
          deleteContentFactoryProducerAttachment(wid, producerSessionKey, attachment.attachment_key)
        )))
      } catch (err) {
        setError(err?.message || '旧会话附件清理失败，请重试。')
        setProducerUploadBusy(false)
        return
      }
      setProducerUploadBusy(false)
    }
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
    setProducerProgress('')
    if (producerStorageKey) {
      try { window.localStorage.setItem(producerStorageKey, nextKey) } catch (_) { /* ignore */ }
    }
  }

  function changeProducerProduct(value) {
    setProducerProductId(value)
    // The displayed proposal belongs to the previous product choice. Make the
    // boundary visible and require a fresh producer turn before confirmation.
    if (producerProposal || producerProposalSha) {
      setProducerProposal(null)
      setProducerProposalSha('')
      setProducerStatus('idle')
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
    const sessionKey = producerSessionKey || newProducerSessionKey()
    setProducerSessionKey(sessionKey)
    if (producerStorageKey) {
      try { window.localStorage.setItem(producerStorageKey, sessionKey) } catch (_) { /* ignore */ }
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
      if (result.selected_product_id) setProducerProductId(String(result.selected_product_id))
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
        setProducerRetryTurnId('')
        setProducerRetryMessage('')
        if (recovered?.selected_product_id) setProducerProductId(String(recovered.selected_product_id))
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

  async function confirmProducerProject() {
    if (!producerSessionKey || !producerProposalSha || producerStatus !== 'proposal_ready') return
    setProducerBusy(true); setError('')
    try {
      const project = await confirmContentFactoryProducerProject(wid, producerSessionKey, producerProposalSha)
      setProducerStatus('created')
      setProducerMessages((current) => [...current, {
        role: 'assistant',
        content: `项目“${project.title}”已创建并开始执行。后续可以直接在右侧查看进度。`,
      }])
      setSelectedKey(project.project_key)
      await refresh()
      setSelectedKey(project.project_key)
    } catch (err) {
      setError(err?.message || '项目创建失败。')
    } finally {
      setProducerBusy(false)
    }
  }

  async function createProject(event) {
    event.preventDefault()
    if (form.content_mode === 'product' && !form.product_id) {
      setError('请先在商品库选择一个商品。')
      return
    }
    if (form.video_duration_min_seconds > form.video_duration_max_seconds) {
      setError('最短时长不能大于最长时长。')
      return
    }
    if (form.video_model === 'omni_flash' && !Array.from({ length: 12 }, (_, index) => (index + 1) * 10).some((value) => value >= form.video_duration_min_seconds && value <= form.video_duration_max_seconds)) {
      setError('Omni 时长范围需包含一个 10 的倍数，例如 10-10 或 15-25。')
      return
    }
    const characterGroups = pendingCharacters.filter((character) => character.files.length)
    const characterFileCount = characterGroups.reduce((total, character) => total + character.files.length, 0)
    if (characterFileCount > 16) {
      setError('每个项目最多上传 16 张人物锚点图。')
      return
    }
    setBusy(true); setError('')
    try {
      const benchmarkFiles = pendingBenchmarkVideoFiles
      const characterFiles = characterGroups.flatMap((character) => character.files)
      const allowReferenceVideo = Boolean(form.allow_reference_video || benchmarkFiles.length)
      const delayAutoRunForCharacterRefs = Boolean(form.auto_run && characterFiles.length && !allowReferenceVideo)
      const payload = {
        ...form,
        allow_reference_video: allowReferenceVideo,
        auto_run: delayAutoRunForCharacterRefs ? false : form.auto_run,
        preferred_browser_device_id: bridge?.selection_required ? null : (selectedBridgeDeviceId || null),
        product_id: form.content_mode === 'product' ? Number(form.product_id) : null,
        brand_name: form.content_mode === 'product' ? selectedProduct?.brand_name : null,
        product_name: form.content_mode === 'product' ? selectedProduct?.product_name : null,
        market: selectedProduct?.market || 'US',
      }
      const project = await createContentFactoryProject(wid, payload)
      for (const character of characterGroups) {
        await uploadContentFactoryAssets(
          wid,
          project.project_key,
          character.files,
          'character_reference',
          {
            character_key: character.key,
            character_name: character.name,
            character_description: character.description,
          },
        )
      }
      if (allowReferenceVideo && benchmarkFiles.length) {
        await uploadContentFactoryAssets(wid, project.project_key, benchmarkFiles, 'reference_video')
      }
      if (delayAutoRunForCharacterRefs) {
        await updateContentFactoryProject(wid, project.project_key, { ...payload, auto_run: true })
        await runContentFactoryStage(wid, project.project_key, {
          instruction: 'Use the uploaded character reference images for visual preview, then continue unattended.',
          stage: project.current_stage,
          runMode: 'continue',
        })
      }
      setForm({
        ...EMPTY_PROJECT_FORM,
        content_mode: form.content_mode,
        product_id: form.content_mode === 'product' ? form.product_id : '',
      })
      setPendingBenchmarkVideoFiles([])
      setPendingCharacters([])
      if (draftStorageKey) window.localStorage.removeItem(draftStorageKey)
      setSelectedKey(project.project_key)
      setTab('factory')
      await refresh()
    } catch (err) { setError(err?.message || '项目创建失败。') } finally { setBusy(false) }
  }

  async function createProduct(event) {
    event.preventDefault()
    setBusy(true); setError('')
    try {
      const product = await createContentFactoryProduct(wid, productForm)
      setProductForm(emptyProductForm)
      setSelectedProductId(String(product.id))
      setForm((current) => ({ ...current, content_mode: 'product', product_id: String(product.id) }))
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
      setForm((current) => ({ ...current, content_mode: 'product', product_id: String(product.id) }))
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
        setForm((current) => ({ ...current, product_id: '' }))
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

  return (
    <div style={{ display: 'grid', gap: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h2 style={{ margin: 0 }}>Content Factory</h2>
          <p className="muted" style={{ margin: '6px 0 0' }}>公司商品库、创意、视觉、视频与投放流水线</p>
        </div>
        <div style={{ ...bridgeTone(bridge), padding: '7px 11px', borderRadius: 6, fontWeight: 600, maxWidth: 620 }}>
          {bridgeStatusText(bridge)}
        </div>
      </div>
      <div className="card" style={{ padding: 12, display: 'grid', gap: 8 }}>
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
              return (
                <div key={device.device_id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6, background: isCurrentDevice ? '#eef5ff' : '#fff', flexWrap: 'wrap' }}>
                  <div style={{ minWidth: 220 }}>
                    <strong>{device.device_name || 'Windows device'}</strong>
                    <span style={{ marginLeft: 8, fontSize: 12, color: device.online ? '#167a3f' : '#6b7280' }}>{device.online ? '在线' : '离线'}</span>
                    {device.connected ? <span style={{ marginLeft: 6, fontSize: 12, color: '#167a3f' }}>CDP 已连接</span> : null}
                    <div className="muted" title={device.device_id} style={{ marginTop: 3, fontSize: 11 }}>{device.device_id} · {device.slot_count || 0} 个 slot{activeCount ? ` · ${activeCount} 个项目运行中` : ''}</div>
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
                  </div>
                </div>
              )
            })}
          </div>
        ) : null}
        <div className="muted">Slot 只属于当前用户的当前设备，不会跨电脑继承。每个并行项目固定占用一个已登录 Slot；请先在对应 Chrome 窗口手动登录 ChatGPT。</div>
      </div>
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
      <div style={{ display: 'flex', gap: 8 }}>
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
                      <button type="button" onClick={() => { setSelectedProductId(String(product.id)); setForm((current) => ({ ...current, content_mode: 'product', product_id: String(product.id) })) }} style={{ border: 'none', background: 'transparent', padding: 0, textAlign: 'left', cursor: 'pointer' }}>
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
                      <button className="btn secondary" type="button" onClick={() => { setSelectedProductId(String(product.id)); changeProducerProduct(String(product.id)); setForm((current) => ({ ...current, content_mode: 'product', product_id: String(product.id) })); setCreationMode('assistant'); setTab('factory') }}>
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
        <div className="content-factory-workspace-layout" style={{ display: 'grid', gridTemplateColumns: 'minmax(300px, 420px) minmax(0, 1fr)', gap: 14 }}>
          <aside className="card" style={{ padding: 14, alignSelf: 'start' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 12 }}>
              <button className={creationMode === 'assistant' ? 'btn' : 'btn secondary'} type="button" onClick={() => setCreationMode('assistant')}>AI制片助理</button>
              <button className={creationMode === 'advanced' ? 'btn' : 'btn secondary'} type="button" onClick={() => setCreationMode('advanced')}>专业参数</button>
            </div>
            {creationMode === 'assistant' ? (
              <section style={{ display: 'grid', gap: 10 }}>
                <div>
                  <strong>和 AI 制片助理聊聊你想做什么视频</strong>
                  <div className="muted" style={{ marginTop: 4, lineHeight: 1.5 }}>
                    像聊天一样描述目标即可。制片助理会追问必要信息，推荐时长、数量和制作方案，经你确认后再创建项目。
                  </div>
                </div>
                {producerSessionLoadError ? (
                  <div style={{ padding: 10, border: '1px solid #e0a000', borderRadius: 8, background: '#fff8e6', display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                    <span style={{ flex: 1 }}>{producerSessionLoadError}</span>
                    <button className="btn secondary" type="button" style={{ width: 'auto', minHeight: 30, padding: '4px 9px' }} onClick={() => setProducerSessionReloadNonce((value) => value + 1)}>
                      重试恢复
                    </button>
                  </div>
                ) : null}
                <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                  <span>关联商品（可选）</span>
                  <select className="input" value={producerProductId} onChange={(event) => changeProducerProduct(event.target.value)} disabled={producerBusy || producerStatus === 'created'}>
                    <option value="">不绑定商品</option>
                    {products.map((product) => <option key={product.id} value={product.id}>{product.brand_name} · {product.product_name}</option>)}
                  </select>
                </label>
                <div style={{ display: 'grid', gap: 7 }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
                    <label className="btn secondary" style={{ width: 'auto', cursor: producerBusy || producerUploadBusy ? 'not-allowed' : 'pointer', fontSize: 12 }}>
                      + 上传对标视频
                      <input
                        type="file"
                        accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm"
                        hidden
                        disabled={producerBusy || producerUploadBusy || producerStatus === 'created'}
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
                        disabled={producerBusy || producerUploadBusy || producerStatus === 'created'}
                        onChange={(event) => uploadProducerAttachments(event, 'character_reference')}
                      />
                    </label>
                  </div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    对标视频最多 1 个、200 MB；人物图最多 16 张、每张 15 MB。一次多选的人物图会作为同一人物的一组参考。
                  </div>
                  {producerUploadBusy ? <div className="muted" style={{ fontSize: 12 }}>正在校验附件，并为对标视频提取关键画面与口播文案…</div> : null}
                  {producerAttachments.length ? (
                    <div style={{ display: 'grid', gap: 6 }}>
                      {producerAttachments.map((attachment) => (
                        <div key={attachment.attachment_key} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 9px', border: '1px solid var(--border)', borderRadius: 8, background: '#fff', fontSize: 12 }}>
                          <span style={{ minWidth: 0, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={attachment.original_name}>
                            {attachmentKindLabel(attachment.kind)} · {attachment.original_name}
                          </span>
                          <span className="muted">{formatAttachmentSize(attachment.size_bytes)}</span>
                          <span className="muted">{attachmentAnalysisLabel(attachment)}</span>
                          <button
                            className="btn secondary"
                            type="button"
                            style={{ width: 'auto', minHeight: 28, padding: '3px 8px', fontSize: 12 }}
                            disabled={producerBusy || producerUploadBusy || producerStatus === 'created'}
                            onClick={() => removeProducerAttachment(attachment.attachment_key)}
                          >
                            移除
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
                <div style={{ maxHeight: 300, overflowY: 'auto', display: 'grid', gap: 8, padding: producerMessages.length ? 8 : 0, border: producerMessages.length ? '1px solid var(--border)' : 'none', borderRadius: 8, background: '#f8fafc' }}>
                  {producerAuthoritativeScriptId ? (
                    <div style={{ padding: '6px 8px', borderRadius: 7, background: '#e9f8ef', color: '#216e3a', fontSize: 12 }}>
                      已识别并锁定你提供的完整文案；后续只改你明确要求修改的部分。
                    </div>
                  ) : null}
                  {!producerMessages.length ? (
                    <div style={{ padding: 10, borderRadius: 8, background: '#eef5ff', color: '#244a7c', fontSize: 13, lineHeight: 1.6 }}>
                      例如：“给已选商品做3条美国 TikTok 强转化动画，开头抓人；价格和 CTA 以我随后提供的原文为准。”其余参数我来判断。
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
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }} aria-label="常用视频需求">
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
                {producerProposal ? (
                  <div style={{ padding: 10, border: '1px solid #9bc2f4', borderRadius: 8, background: '#f5f9ff', display: 'grid', gap: 6, fontSize: 13 }}>
                    <strong>建议制作方案</strong>
                    <div><span className="muted">目标：</span>{producerProposal.content_objective}</div>
                    <div><span className="muted">规格：</span>{producerProposal.video_count} 条 · {producerProposal.video_duration_min_seconds}-{producerProposal.video_duration_max_seconds} 秒 · {producerProposal.video_aspect_ratio}</div>
                    <div><span className="muted">风格：</span>{producerProposal.visual_style}</div>
                    <div><span className="muted">节奏：</span>{producerProposal.pacing}</div>
                    <div><span className="muted">声音：</span>{producerProposal.audio_direction}</div>
                    {producerProposal.conversion_direction ? <div><span className="muted">转化：</span>{producerProposal.conversion_direction}</div> : null}
                    <div className="muted">确认只会创建一个项目；商品事实仍以公司商品库为准。</div>
                  </div>
                ) : null}
                {producerStatus !== 'created' ? (
                  <form onSubmit={sendProducerMessage} style={{ display: 'grid', gap: 7 }}>
                    <textarea className="input" rows={4} maxLength={50000} placeholder="描述目标、现有文案、受众或你特别在意的要求……" value={producerInput} onChange={(event) => setProducerInput(event.target.value)} disabled={producerBusy} />
                    <button className="btn" disabled={producerBusy || producerUploadBusy || producerAttachmentAnalysisPending || !producerInput.trim()}>{producerMessages.length ? '继续沟通' : '开始和制片助理沟通'}</button>
                  </form>
                ) : null}
                {producerStatus === 'proposal_ready' && producerProposalSha ? (
                  <button className="btn" type="button" onClick={confirmProducerProject} disabled={producerBusy}>确认并创建 {producerProposal?.video_count || ''} 条视频任务</button>
                ) : null}
                <button className="btn secondary" type="button" onClick={resetProducerConversation} disabled={producerBusy || producerUploadBusy}>开始新的需求</button>
              </section>
            ) : null}
            <form onSubmit={createProject} style={{ display: creationMode === 'advanced' ? 'grid' : 'none', gap: 8 }}>
              <strong>新建内容项目</strong>
              <input className="input" placeholder="项目名称" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} required />
              <textarea className="input" rows={2} maxLength={255} placeholder="这批视频要达成什么目标？例如：解释一个知识、完成一个教程、讲好一组故事，或帮助观众选择一项产品。" value={form.content_objective} onChange={(e) => setForm({ ...form, content_objective: e.target.value })} />
              <textarea className="input" rows={2} maxLength={1000} placeholder="目标受众：写真实人群、处境、认知和购买顾虑，不要只写年龄性别。" value={form.target_audience} onChange={(e) => setForm({ ...form, target_audience: e.target.value })} />
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>是否绑定商品</span>
                <select className="input" value={form.content_mode} onChange={(e) => setForm({ ...form, content_mode: e.target.value })}>
                  <option value="product">绑定商品</option>
                  <option value="general">不绑定商品</option>
                </select>
              </label>
              {form.content_mode === 'product' ? <>
                <select className="input" value={form.product_id || ''} onChange={(e) => { setSelectedProductId(e.target.value); setForm({ ...form, product_id: e.target.value }) }} required>
                  <option value="">选择商品库商品</option>
                  {products.map((product) => <option key={product.id} value={product.id}>{product.brand_name} · {product.product_name}</option>)}
                </select>
                {selectedProduct ? <div style={{ padding: 10, border: '1px solid var(--border)', borderRadius: 6, background: '#f8fbff' }}>
                  <strong>{selectedProduct.product_name}</strong>
                  <div className="muted" style={{ marginTop: 4 }}>{factsLabel(selectedProduct)}</div>
                </div> : null}
              </> : <div className="muted" style={{ padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6 }}>
                不绑定商品。流水线不会上传或虚构产品、包装、卖点、价格和购买 CTA。
              </div>}
              <textarea className="input" rows={4} placeholder={form.content_mode === 'product' ? '补充产品资料和必须遵守的限制。创意形式、段数和场景由编导根据目标决定，不需要写母版。' : '补充真实资料和必须遵守的限制。创意形式、段数和场景由编导根据目标决定。'} value={form.product_brief} onChange={(e) => setForm({ ...form, product_brief: e.target.value })} />
              <div className="muted" style={{ padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6 }}>
                通用编导已启用：实际内容形式由目标和资料决定，可生成故事、科普、教程、演示、对比、访谈、动画等，不使用固定母版。先锁定完整文案并通过独立审核，再生成媒体。
              </div>
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>裂变视频数量</span>
                <input className="input" type="number" min="1" max="50" value={form.video_count} onChange={(e) => setForm({ ...form, video_count: Number(e.target.value) || 10 })} />
              </label>
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>同时生成视频数</span>
                <select className="input" value={form.max_api_video_variants_in_flight} onChange={(e) => setForm({ ...form, max_api_video_variants_in_flight: Number(e.target.value) })}>
                  {[1, 2, 3, 4].map((count) => <option key={count} value={count}>{count} 条</option>)}
                </select>
                <span className="muted">只并行媒体供应商、下载和拼接；编导仍按单条隔离。</span>
              </label>
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>目标视频时长范围（秒）</span>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr auto 1fr', alignItems: 'center', gap: 6 }}>
                  <input className="input" aria-label="最短时长" type="number" min="1" max="120" value={form.video_duration_min_seconds} onChange={(e) => setForm({ ...form, video_duration_min_seconds: Number(e.target.value) || 1 })} />
                  <span className="muted">至</span>
                  <input className="input" aria-label="最长时长" type="number" min="1" max="120" value={form.video_duration_max_seconds} onChange={(e) => setForm({ ...form, video_duration_max_seconds: Number(e.target.value) || 1 })} />
                </div>
              </label>
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>视频生成模型</span>
                <select className="input" value={form.video_model} onChange={(e) => { const model = e.target.value; const max = videoReferenceLimit(model); setForm({ ...form, video_model: model, video_reference_limit: Math.min(form.video_reference_limit, max) }) }}>
                  <option value="omni_flash">Omni Flash</option>
                  <option value="seedance_2_0_mini">Seedance 2.0 Mini</option>
                </select>
              </label>
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>生成分辨率</span>
                <select className="input" value={form.video_resolution} onChange={(e) => setForm({ ...form, video_resolution: e.target.value })}>
                  <option value="720p">720p</option>
                  <option value="480p">480p</option>
                </select>
              </label>
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>画面比例</span>
                <select className="input" value={form.video_aspect_ratio} onChange={(e) => setForm({ ...form, video_aspect_ratio: e.target.value })}>
                  <option value="9:16">9:16 竖屏</option>
                  <option value="16:9">16:9 横屏</option>
                  <option value="1:1">1:1 方形</option>
                </select>
              </label>
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>视频语言</span>
                <select className="input" value={form.video_language} onChange={(e) => setForm({ ...form, video_language: e.target.value })}>
                  <option value="en-US">English (US)</option>
                  <option value="zh-CN">简体中文</option>
                </select>
              </label>
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>单段最多参考图</span>
                <select className="input" value={form.video_reference_limit} onChange={(e) => setForm({ ...form, video_reference_limit: Number(e.target.value) })}>
                  {Array.from({ length: videoReferenceLimit(form.video_model) }, (_, index) => index + 1).map((count) => <option key={count} value={count}>{count} 张</option>)}
                </select>
              </label>
              <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                <span>参考帧模式</span>
                <select className="input" value={form.video_frame_mode} onChange={(e) => setForm({ ...form, video_frame_mode: e.target.value })}>
                  <option value="reference">多参考图</option>
                  <option value="first_last">首尾帧</option>
                </select>
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                <input type="checkbox" checked={form.allow_reference_video} onChange={(e) => setForm({ ...form, allow_reference_video: e.target.checked })} />
                启用对标视频模仿
              </label>
              <label style={{ display: 'grid', gap: 5, padding: 10, border: '1px dashed #9bbce7', borderRadius: 6, background: '#f7fbff', cursor: 'pointer', fontSize: 13 }}>
                <strong>上传对标视频（可选）</strong>
                <span className="muted">MP4 / MOV / WebM，上传后会先提取文案和关键帧。</span>
                <input type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm" onChange={(event) => { const files = Array.from(event.target.files || []); setPendingBenchmarkVideoFiles(files); if (files.length) setForm({ ...form, allow_reference_video: true }) }} disabled={busy} style={{ display: 'none' }} />
                {pendingBenchmarkVideoFiles.length ? <span className="muted">{pendingBenchmarkVideoFiles.map((file) => file.name).join('、')}</span> : null}
              </label>
              <section style={{ display: 'grid', gap: 8, padding: 10, border: '1px dashed #9bbce7', borderRadius: 6, background: '#f7fbff', fontSize: 13 }}>
                <div>
                  <strong>人物锚点（可选）</strong>
                  <div className="muted" style={{ marginTop: 3 }}>每个人物可上传多张不同角度图片，并填写人物描述。最多共 16 张。</div>
                </div>
                {pendingCharacters.map((character, index) => (
                  <div key={character.key} style={{ display: 'grid', gap: 6, padding: 8, border: '1px solid var(--border)', borderRadius: 6, background: '#fff' }}>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <input className="input" value={character.name} placeholder={`人物 ${index + 1} 名称`} onChange={(event) => setPendingCharacters((items) => items.map((item) => item.key === character.key ? { ...item, name: event.target.value } : item))} />
                      <button className="btn secondary" type="button" onClick={() => setPendingCharacters((items) => items.filter((item) => item.key !== character.key))} disabled={busy}>删除</button>
                    </div>
                    <textarea className="input" rows={2} maxLength={2000} value={character.description} placeholder="人物描述：年龄范围、脸型、发型、身材、服装、角色关系等" onChange={(event) => setPendingCharacters((items) => items.map((item) => item.key === character.key ? { ...item, description: event.target.value } : item))} />
                    <label style={{ display: 'grid', gap: 4, padding: 8, border: '1px dashed #9bbce7', borderRadius: 6, cursor: 'pointer' }}>
                      <strong>选择该人物的图片（可多张）</strong>
                      <input type="file" multiple accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" onChange={(event) => setPendingCharacters((items) => items.map((item) => item.key === character.key ? { ...item, files: Array.from(event.target.files || []) } : item))} disabled={busy} style={{ display: 'none' }} />
                      <span className="muted">{character.files.length ? `${character.files.length} 张：${character.files.map((file) => file.name).join('、')}` : '尚未选择图片'}</span>
                    </label>
                  </div>
                ))}
                <button className="btn secondary" type="button" onClick={() => setPendingCharacters((items) => [...items, newCharacterDraft(items.length + 1)])} disabled={busy}>+ 添加人物</button>
              </section>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                <input type="checkbox" checked={form.auto_run} onChange={(e) => setForm({ ...form, auto_run: e.target.checked })} />
                无人值守自动执行
              </label>
              <button className="btn" disabled={busy}>创建项目</button>
            </form>
            <div style={{ borderTop: '1px solid var(--border)', margin: '14px 0 10px' }} />
            <div style={{ display: 'grid', gap: 6 }}>
              {projects.map((project) => <button key={project.project_key} type="button" onClick={() => setSelectedKey(project.project_key)} style={{ textAlign: 'left', padding: 10, borderRadius: 6, border: project.project_key === selected?.project_key ? '1px solid #3b82f6' : '1px solid var(--border)', background: project.project_key === selected?.project_key ? '#eef5ff' : 'transparent', cursor: 'pointer' }}><strong>{project.title}</strong><div className="muted" style={{ marginTop: 4 }}>{projectStatusText(project)}</div></button>)}
              {!projects.length ? <span className="muted">暂无项目</span> : null}
            </div>
          </aside>
          <main className="card" style={{ padding: 14, minWidth: 0 }}>
            {selected ? <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10, paddingBottom: 10, borderBottom: '1px solid var(--border)' }}>
                <div className="muted" style={{ fontSize: 12 }}>
                绑定 Bridge {selected.browser_slot ?? '未分配'}
                {' · '}
                目标时长 {selected.config_json?.video_duration_min_seconds || selected.config_json?.video_duration_seconds || 10}-{selected.config_json?.video_duration_max_seconds || selected.config_json?.video_duration_seconds || 10} 秒
                {' · '}{videoSegmentLabel(selected.config_json?.video_model)}
                {' · '}分辨率 {selected.config_json?.video_resolution || '720p'}
                {' · '}比例 {selected.config_json?.video_aspect_ratio || selected.config_json?.director_series_brief?.aspect_ratio || '9:16'}
                {' · '}语言 {selected.config_json?.video_language === 'zh-CN' ? '简体中文' : 'English (US)'}
              </div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <button className="btn secondary" type="button" onClick={() => setProjectEditOpen((value) => !value)} disabled={busy}>编辑项目</button>
                {selected.config_json?.allow_reference_video ? <label className="btn secondary" style={{ cursor: 'pointer' }}>上传对标视频<input type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm" onChange={(event) => uploadFiles(event, 'reference_video')} disabled={busy} style={{ display: 'none' }} /></label> : null}
                <button className="btn secondary" type="button" onClick={() => setCharacterUploadOpen((value) => !value)} disabled={busy}>管理人物锚点</button>
                {selected.status === 'paused' ? (
                  <button className="btn" type="button" onClick={resumeProject} disabled={busy}>恢复项目</button>
                ) : (
                  <button className="btn secondary" type="button" onClick={pauseProject} disabled={busy || ['complete'].includes(selected.status)}>暂停项目</button>
                )}
                <button className="btn secondary" type="button" onClick={deleteProject} disabled={busy} style={{ color: '#c93636' }}>删除项目</button>
              </div>
            </div> : null}
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
            {!selected ? <div className="muted">创建项目后开始。</div> : <>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'start', flexWrap: 'wrap' }}>
                <div><h3 style={{ margin: 0 }}>{selected.title}</h3><div className="muted" style={{ marginTop: 5 }}>{selected.product_name || '非商品内容'} · {selected.market} · {selected.project_key}</div><div style={{ ...statusTone(selected.status), display: 'inline-block', marginTop: 7, padding: '4px 8px', borderRadius: 10, fontSize: 12 }}>{projectStatusText(selected)}</div></div>
                <button className="btn secondary" type="button" onClick={refresh}>刷新</button>
              </div>
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
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>视频模型</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_model} onChange={(e) => { const model = e.target.value; setProjectEditForm({ ...projectEditForm, video_model: model, video_reference_limit: Math.min(projectEditForm.video_reference_limit, videoReferenceLimit(model)) }) }}><option value="omni_flash">Omni Flash</option><option value="seedance_2_0_mini">Seedance 2.0 Mini</option></select></label>
                    <textarea className="input" disabled={mediaContractLocked} rows={2} maxLength={255} aria-label="内容目标" placeholder="这批视频要达成什么目标？" value={projectEditForm.content_objective || ''} onChange={(e) => setProjectEditForm({ ...projectEditForm, content_objective: e.target.value })} style={{ gridColumn: '1 / -1' }} />
                    <textarea className="input" disabled={mediaContractLocked} rows={2} maxLength={1000} aria-label="目标受众" placeholder="目标受众、处境、认知和购买顾虑" value={projectEditForm.target_audience || ''} onChange={(e) => setProjectEditForm({ ...projectEditForm, target_audience: e.target.value })} style={{ gridColumn: '1 / -1' }} />
                    <textarea className="input" disabled={mediaContractLocked} rows={4} aria-label="事实与限制" placeholder="补充真实资料和必须遵守的限制；不要写固定段数或场景母版。" value={projectEditForm.product_brief} onChange={(e) => setProjectEditForm({ ...projectEditForm, product_brief: e.target.value })} style={{ gridColumn: '1 / -1' }} />
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>最短时长（秒）</span><input className="input" disabled={mediaContractLocked} type="number" min="1" max="120" value={projectEditForm.video_duration_min_seconds} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_duration_min_seconds: Number(e.target.value) || 1 })} /></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>最长时长（秒）</span><input className="input" disabled={mediaContractLocked} type="number" min="1" max="120" value={projectEditForm.video_duration_max_seconds} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_duration_max_seconds: Number(e.target.value) || 1 })} /></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>分辨率</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_resolution} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_resolution: e.target.value })}><option value="720p">720p</option><option value="480p">480p</option></select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>画面比例</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_aspect_ratio} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_aspect_ratio: e.target.value })}><option value="9:16">9:16 竖屏</option><option value="16:9">16:9 横屏</option><option value="1:1">1:1 方形</option></select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>语言</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_language} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_language: e.target.value })}><option value="en-US">English (US)</option><option value="zh-CN">简体中文</option></select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>单段参考图上限</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_reference_limit} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_reference_limit: Number(e.target.value) })}>{Array.from({ length: videoReferenceLimit(projectEditForm.video_model) }, (_, index) => index + 1).map((count) => <option key={count} value={count}>{count} 张</option>)}</select></label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 12 }}><span>参考帧模式</span><select className="input" disabled={mediaContractLocked} value={projectEditForm.video_frame_mode} onChange={(e) => setProjectEditForm({ ...projectEditForm, video_frame_mode: e.target.value })}><option value="reference">多参考图</option><option value="first_last">首尾帧</option></select></label>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12 }}><input type="checkbox" disabled={mediaContractLocked} checked={projectEditForm.allow_reference_video} onChange={(e) => setProjectEditForm({ ...projectEditForm, allow_reference_video: e.target.checked })} />启用对标视频模仿</label>
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
              {paused ? <div style={{ margin: '0 0 12px', padding: 12, border: '1px solid #f0c36d', borderRadius: 6, background: '#fffaf0', display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}><div><strong>已暂停，可人工干预</strong><div className="muted" style={{ marginTop: 4 }}>{selected.last_error || '检查编导、API、额度或素材后，点恢复项目从断点继续；浏览器只在兜底时需要在线。'}</div></div><button className="btn" type="button" onClick={resumeProject} disabled={busy}>恢复执行</button></div> : null}
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
