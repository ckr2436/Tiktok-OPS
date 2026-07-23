// Model-based AI video generation page.
import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { message } from 'antd'
import FormField from '../../../../components/ui/FormField.jsx'
import Loading from '../../../../components/ui/Loading.jsx'
import aiVideoApi from '../service.js'
import Modal from '../../../../components/ui/Modal.jsx'

const MAX_PROMPT_LEN = 10_000
// Persisted storage identifiers stay stable so an upgrade never discards drafts.
const LAST_TASK_KEY_PREFIX = 'kie_sora2_last_task_'
const FORM_STATE_KEY_PREFIX = 'kie_sora2_form_state_'
const FORM_FILE_DB_NAME = 'gmv_kie_ai_video_form_state'
const FORM_FILE_STORE = 'files'
const MAX_HISTORY_TOTAL = 500

const DEFAULT_MODEL_ID = 'omni_flash'

function normalizeModelId(value) {
  if (['seedance_2_0_mini', 'seedance_2_0', 'seedence_2_0_mini', 'doubao-seedance-2-0-mini-260615'].includes(value)) {
    return 'seedance_2_0_mini'
  }
  return 'omni_flash'
}

function getLastTaskKey(wid, modelId) {
  return `${LAST_TASK_KEY_PREFIX}${wid ?? ''}_${modelId}`
}

function getFormStateKey(wid) {
  return `${FORM_STATE_KEY_PREFIX}${wid ?? ''}`
}

const BANDIANWA_ASPECT_OPTIONS = [
  { value: 'portrait', label: '竖屏 9:16' },
  { value: 'landscape', label: '横屏 16:9' },
  { value: 'square', label: '方屏 1:1' },
]

const BANDIANWA_SECONDS_OPTIONS = [
  { value: 8, label: '8 秒' },
  { value: 10, label: '10 秒' },
]

const SEEDANCE_SECONDS_OPTIONS = [1, 2, 3, 5, 8, 10, 12, 15].map((value) => ({
  value,
  label: `${value} 秒`,
}))

const PAGE_SIZE_OPTIONS = [10, 20, 50]

const MODEL_CONFIGS = [
  {
    id: 'omni_flash',
    label: 'Omni Flash',
    kind: 'bandianwa-omni',
    submitPath: 'videos',
  },
  {
    id: 'seedance_2_0_mini',
    label: 'Seedance 2.0 Mini',
    kind: 'volcengine-seedance-mini',
    referenceImageLimit: 9,
  },
]

function Badge({ type = 'default', children }) {
  const colorMap = {
    waiting: '#999',
    running: '#0d6efd',
    success: '#16a34a',
    fail: '#dc2626',
    timeout: '#f97316',
    default: '#666',
  }
  const bg = `${colorMap[type] || colorMap.default}22`
  const border = `${colorMap[type] || colorMap.default}44`
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '2px 8px',
        borderRadius: 999,
        fontSize: 12,
        color: colorMap[type] || colorMap.default,
        backgroundColor: bg,
        border: `1px solid ${border}`,
      }}
    >
      {children}
    </span>
  )
}

function shouldPollByState(state) {
  if (!state) return true
  const s = String(state).toLowerCase()

  if (
    s.includes('success') ||
    s.includes('succeeded') ||
    s.includes('complete') ||
    s === 'ok' ||
    s.includes('fail') ||
    s.includes('error') ||
    s.includes('timeout')
  ) {
    return false
  }

  if (
    s.includes('download') ||
    s.includes('wait') ||
    s.includes('queue') ||
    s.includes('run') ||
    s.includes('progress') ||
    s.includes('process') ||
    s.includes('gen')
  ) {
    return true
  }

  return false
}

function statusBadgeType(state) {
  const s = String(state || '').toLowerCase()
  if (!s) return 'default'
  if (s.includes('download')) return 'running'
  if (s.includes('wait') || s.includes('queue')) return 'waiting'
  if (s.includes('run') || s.includes('process') || s.includes('progress') || s.includes('gen')) {
    return 'running'
  }
  if (s === 'success' || s === 'succeeded' || s === 'ok' || s.includes('complete')) {
    return 'success'
  }
  if (s.includes('timeout')) return 'timeout'
  if (s.includes('fail') || s.includes('error')) return 'fail'
  return 'default'
}

function canRetryTask(task) {
  const s = String(task?.state || '').toLowerCase()
  return s === 'failed' || s === 'error' || s === 'timeout'
}

function canRegenerateTask(task) {
  const s = String(task?.state || '').toLowerCase()
  return s === 'success' || s === 'failed' || s === 'error' || s === 'timeout'
}

function canDeleteTask(task) {
  const s = String(task?.state || '').toLowerCase()
  return s === 'success' || s === 'failed' || s === 'error' || s === 'timeout'
}

function canBatchDownloadTask(task) {
  const s = String(task?.state || '').toLowerCase()
  return s === 'success' || s === 'succeeded' || s === 'ok' || s.includes('complete')
}

function toBandianwaAspectRatio(value) {
  if (value === 'landscape') return '16:9'
  if (value === 'square') return '1:1'
  return '9:16'
}

function createBandianwaDraft(seed = {}) {
  return {
    id: `bdw-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    prompt: seed.prompt || '',
    aspectRatio: seed.aspectRatio || 'portrait',
    seconds: seed.seconds || 8,
    files: seed.files || [],
    serverReferenceFilePaths: seed.serverReferenceFilePaths || [],
    serverReferenceFiles: seed.serverReferenceFiles || [],
  }
}

function makeImagePreview(file) {
  return {
    id: `img-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    file,
    previewUrl: URL.createObjectURL(file),
  }
}

function cloneDraftImage(img) {
  return {
    ...img,
    id: `img-${Date.now()}-${Math.random().toString(16).slice(2)}`,
  }
}

function normalizeServerReferenceFiles(paths = [], files = []) {
  const byPath = new Map()
  for (const file of files || []) {
    if (file?.path) byPath.set(String(file.path), file)
  }
  return (paths || [])
    .map((ref, index) => {
      const path = typeof ref === 'string' ? ref : ref?.path
      if (!path) return null
      const existing = byPath.get(String(path))
      return {
        id: existing?.id || `server-img-${Date.now()}-${index}-${Math.random().toString(16).slice(2)}`,
        fileId: existing?.fileId,
        path,
        filename: existing?.filename || ref?.filename || `参考图 ${index + 1}`,
        previewUrl: existing?.previewUrl || '',
      }
    })
    .filter(Boolean)
}

// 小工具：并发限制的批量执行（最多 concurrency 个 worker 同时跑）
function formatTaskCreator(task) {
  if (!task) return ''
  const label =
    task.created_by_label ||
    task.created_by_display_name ||
    task.created_by_username ||
    task.created_by_usercode
  if (label && task.created_by_usercode && label !== task.created_by_usercode) {
    return `${label}（${task.created_by_usercode}）`
  }
  if (label) return label
  if (task.created_by_user_id) return `用户 #${task.created_by_user_id}`
  return '历史任务'
}

function openFormFileDb() {
  if (typeof indexedDB === 'undefined') {
    return Promise.resolve(null)
  }
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(FORM_FILE_DB_NAME, 1)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(FORM_FILE_STORE)) {
        db.createObjectStore(FORM_FILE_STORE, { keyPath: 'id' })
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

async function loadFormFileState(id) {
  const db = await openFormFileDb()
  if (!db) return null
  return new Promise((resolve, reject) => {
    const tx = db.transaction(FORM_FILE_STORE, 'readonly')
    const req = tx.objectStore(FORM_FILE_STORE).get(id)
    req.onsuccess = () => resolve(req.result || null)
    req.onerror = () => reject(req.error)
    tx.oncomplete = () => db.close()
    tx.onerror = () => db.close()
  })
}

async function saveFormFileState(id, payload) {
  const db = await openFormFileDb()
  if (!db) return
  await new Promise((resolve, reject) => {
    const tx = db.transaction(FORM_FILE_STORE, 'readwrite')
    tx.objectStore(FORM_FILE_STORE).put({
      id,
      ...payload,
      updatedAt: Date.now(),
    })
    tx.oncomplete = resolve
    tx.onerror = () => reject(tx.error)
  })
  db.close()
}

async function runWithConcurrency(items, concurrency, worker) {
  const results = new Array(items.length)
  const total = items.length
  const limit = Math.max(1, Math.min(concurrency, total))

  let index = 0

  async function runner() {
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const current = index
      if (current >= total) break
      index += 1
      results[current] = await worker(items[current], current)
    }
  }

  const workers = []
  for (let i = 0; i < limit; i += 1) {
    workers.push(runner())
  }
  await Promise.all(workers)
  return results
}

function ConfirmDialog({ open, title, content, onCancel, onOk }) {
  return (
    <Modal open={open} onClose={onCancel} title={title} maskClosable>
      <p style={{ marginTop: 0, lineHeight: 1.6 }}>{content}</p>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
        <button type="button" className="btn ghost" onClick={onCancel}>
          取消
        </button>
        <button
          type="button"
          className="btn danger"
          onClick={async () => {
            await onOk?.()
            onCancel?.()
          }}
        >
          确认
        </button>
      </div>
    </Modal>
  )
}

export default function GenerateVideoPage() {
  const { wid } = useParams()
  const queryClient = useQueryClient()

  const [modelId, setModelId] = useState(DEFAULT_MODEL_ID)

  const currentModel = useMemo(
    () => MODEL_CONFIGS.find((m) => m.id === modelId) || MODEL_CONFIGS[0],
    [modelId],
  )
  const referenceImageLimit = currentModel.referenceImageLimit || 7
  const aspectOptions = BANDIANWA_ASPECT_OPTIONS

  const [bandianwaDrafts, setBandianwaDrafts] = useState(() => [
    createBandianwaDraft(),
  ])

  const [submitting, setSubmitting] = useState(false)
  const [singleSubmittingDraftId, setSingleSubmittingDraftId] = useState(null)
  const [err, setErr] = useState('')
  const [confirmState, setConfirmState] = useState({ open: false })
  const [regenerateTarget, setRegenerateTarget] = useState(null)

  const showSuccessToast = (msg) => {
    message.success(msg)
  }

  const showErrorToast = (msg) => {
    message.error(msg)
  }

  const { data: routingStatus } = useQuery({
    queryKey: ['ai-video-model-routing', wid],
    queryFn: () => aiVideoApi.getProviderStatus(wid),
    enabled: Boolean(wid),
    staleTime: 15_000,
    refetchInterval: 30_000,
  })
  const modelRoutes = useMemo(
    () => Object.fromEntries((routingStatus?.models || []).map((item) => [item.id, item])),
    [routingStatus],
  )
  const isModelAvailable = (id) => routingStatus == null || modelRoutes[id]?.available === true

  // 当前任务 ID
  const [currentTaskId, setCurrentTaskId] = useState(null)
  const [taskIdSetAt, setTaskIdSetAt] = useState(null)
  const firstTaskRefreshRef = useRef(false)
  const skipNextModelResetRef = useRef(false)
  const [formStateReady, setFormStateReady] = useState(false)

  // 历史分页
  const [pageSize, setPageSize] = useState(10)
  const [page, setPage] = useState(1)
  const [selectedDownloadTaskIds, setSelectedDownloadTaskIds] = useState([])
  const [batchDownloading, setBatchDownloading] = useState(false)

  // 预览弹窗（视频）
  const [preview, setPreview] = useState(null) // { url, kind, mime }

  const lastTaskKey = useMemo(
    () => getLastTaskKey(wid, modelId),
    [wid, modelId],
  )
  const formStateKey = useMemo(() => getFormStateKey(wid), [wid])

  // 模型切换时重置部分状态
  useEffect(() => {
    if (skipNextModelResetRef.current) {
      skipNextModelResetRef.current = false
      return
    }
    setErr('')
    setCurrentTaskId(null)
    setRegenerateTarget(null)
    setPreview(null)
    setPage(1)

    setBandianwaDrafts([createBandianwaDraft()])
  }, [modelId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!formStateKey || typeof window === 'undefined') return undefined
    let cancelled = false
    setFormStateReady(false)

    async function restoreFormState() {
      let saved = null
      try {
        const raw = window.localStorage.getItem(formStateKey)
        saved = raw ? JSON.parse(raw) : null
      } catch {
        saved = null
      }

      let savedFiles = null
      try {
        savedFiles = await loadFormFileState(formStateKey)
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn('restore AI video files failed', error)
      }

      if (cancelled) return

      if (saved) {
        const restoredModelId = normalizeModelId(saved.modelId)
        if (restoredModelId !== modelId) {
          skipNextModelResetRef.current = true
          setModelId(restoredModelId)
        }
        setPageSize(Number(saved.pageSize) || 10)

        const draftFilesById = new Map(
          (savedFiles?.draftFiles || []).map((entry) => [entry.draftId, entry.files || []]),
        )
        const restoredDrafts =
          Array.isArray(saved.bandianwaDrafts) && saved.bandianwaDrafts.length
            ? saved.bandianwaDrafts.map((item) => {
                const draft = createBandianwaDraft(item)
                return {
                  ...draft,
                  id: item.id || draft.id,
                  serverReferenceFilePaths: item.serverReferenceFilePaths || [],
                  serverReferenceFiles: normalizeServerReferenceFiles(
                    item.serverReferenceFilePaths || [],
                    item.serverReferenceFiles || [],
                  ),
                  files: (draftFilesById.get(item.id) || [])
                    .filter((img) => img?.file)
                    .map((img) => ({
                      id: img.id || `img-${Date.now()}-${Math.random().toString(16).slice(2)}`,
                      file: img.file,
                      previewUrl: URL.createObjectURL(img.file),
                    })),
                }
              })
            : [createBandianwaDraft()]
        setBandianwaDrafts(restoredDrafts)
      }

      setFormStateReady(true)
    }

    restoreFormState()
    return () => {
      cancelled = true
    }
  }, [formStateKey]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!formStateReady || !formStateKey || typeof window === 'undefined') return undefined
    const timer = window.setTimeout(() => {
      const state = {
        modelId,
        pageSize,
        bandianwaDrafts: bandianwaDrafts.map((item) => ({
          id: item.id,
          prompt: item.prompt || '',
          aspectRatio: item.aspectRatio || 'portrait',
          seconds: item.seconds || 8,
          serverReferenceFilePaths: item.serverReferenceFilePaths || [],
          serverReferenceFiles: (item.serverReferenceFiles || []).map((img) => ({
            id: img.id,
            fileId: img.fileId,
            path: img.path,
            filename: img.filename,
            previewUrl: img.previewUrl,
          })),
        })),
        savedAt: Date.now(),
      }
      try {
        window.localStorage.setItem(formStateKey, JSON.stringify(state))
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn('save AI video form state failed', error)
      }
      saveFormFileState(formStateKey, {
        draftFiles: bandianwaDrafts.map((item) => ({
          draftId: item.id,
          files: (item.files || [])
            .filter((img) => img?.file)
            .map((img) => ({ id: img.id, file: img.file })),
        })),
      }).catch((error) => {
        // eslint-disable-next-line no-console
        console.warn('save AI video files failed', error)
      })
    }, 350)
    return () => window.clearTimeout(timer)
  }, [
    formStateReady,
    formStateKey,
    modelId,
    pageSize,
    bandianwaDrafts,
  ])

  // 记录当前任务 ID 最近一次被设置的时间（用于避免创建后立即查询导致的 404）
  useEffect(() => {
    if (currentTaskId == null) {
      setTaskIdSetAt(null)
      return
    }
    setTaskIdSetAt(Date.now())
  }, [currentTaskId])

  useEffect(() => {
    firstTaskRefreshRef.current = false
  }, [currentTaskId])

  // 挂载 / 模型切换时恢复最近任务
  useEffect(() => {
    if (!wid || typeof window === 'undefined') return
    const lastId = window.localStorage.getItem(lastTaskKey)
    if (lastId) {
      setCurrentTaskId(Number(lastId) || lastId)
    } else {
      setCurrentTaskId(null)
    }
  }, [wid, lastTaskKey])

  // pageSize 变化回到第一页
  useEffect(() => {
    setPage(1)
  }, [pageSize])

  // 拦截全局 http:error 事件，避免因任务未落库产生阻塞式弹窗
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const handler = (e) => {
      if (e?.detail?.message === 'Task not found') {
        e.preventDefault?.()
      }
    }
    window.addEventListener('http:error', handler)
    return () => window.removeEventListener('http:error', handler)
  }, [])

  // ------- React Query：当前任务 -------
  const taskQuery = useQuery({
    queryKey: ['ai-video-task', wid, modelId, currentTaskId],
    queryFn: async () => {
      const res = await aiVideoApi.getTask(wid, currentTaskId, {
        refresh: firstTaskRefreshRef.current,
      })
      if (!firstTaskRefreshRef.current) {
        firstTaskRefreshRef.current = true
      }
      return res
    },
    enabled: !!wid && !!currentTaskId,
    refetchInterval: (query) => {
      const state = query?.state?.data?.state
      return shouldPollByState(state) ? 8000 : false
    },
  })

  const {
    data: task,
    isLoading: loadingTask,
    error: taskError,
    refetch: refetchTask,
  } = taskQuery

  const enableFilesQuery =
    !!wid &&
    !!currentTaskId &&
    !!task &&
    !shouldPollByState(task.state)

  // 如果后端返回 404（getTask → null），自动清理本地“当前任务”状态和缓存
  useEffect(() => {
    if (!currentTaskId) return
    if (loadingTask) return
    if (task !== null) return // null 表示 404；undefined 是还没拉到

    // 刚创建完任务时，后端可能尚未落库，短时间内的 404 需要宽容处理
    const justSetTask = taskIdSetAt && Date.now() - taskIdSetAt < 15_000
    if (justSetTask) return

    setCurrentTaskId(null)
    setPreview(null)

    if (typeof window !== 'undefined') {
      window.localStorage.removeItem(lastTaskKey)
    }

    queryClient.removeQueries({ queryKey: ['ai-video-task', wid, modelId] })
    queryClient.removeQueries({ queryKey: ['ai-video-files', wid, modelId] })
  }, [
    currentTaskId,
    loadingTask,
    task,
    lastTaskKey,
    wid,
    modelId,
    queryClient,
    taskIdSetAt,
  ])

  // ------- React Query：当前任务文件 -------
  const { data: files = [], refetch: refetchFiles } = useQuery({
    queryKey: ['ai-video-files', wid, modelId, currentTaskId],
    queryFn: () => aiVideoApi.listTaskFiles(wid, currentTaskId),
    enabled: enableFilesQuery,
    refetchInterval: false,
  })

  // ------- React Query：任务历史 -------
  const historyQuery = useQuery({
    queryKey: ['ai-video-history', wid, modelId, page, pageSize],
    queryFn: () => aiVideoApi.listTasks(wid, {
      page,
      size: pageSize,
      model: modelId,
      refresh_pending: true,
    }),
    enabled: !!wid,
    refetchInterval: (query) => {
      const items = query?.state?.data?.items || []
      const hasPending = items.some((item) => shouldPollByState(item.state))
      return hasPending ? 8000 : false
    },
    keepPreviousData: true,
  })

  const {
    data: historyResp,
    isLoading: historyLoading,
    refetch: refetchHistory,
  } = historyQuery

  const rawTotal = historyResp?.total ?? 0
  const historyTotal = Math.min(rawTotal, MAX_HISTORY_TOTAL)
  const history = historyResp?.items || []
  const downloadableHistoryIds = useMemo(
    () => history.filter((item) => canBatchDownloadTask(item)).map((item) => Number(item.id)),
    [history],
  )
  const selectedDownloadIds = useMemo(() => {
    const allowed = new Set(downloadableHistoryIds)
    return selectedDownloadTaskIds.filter((id) => allowed.has(Number(id)))
  }, [downloadableHistoryIds, selectedDownloadTaskIds])
  const allDownloadableSelected =
    downloadableHistoryIds.length > 0 &&
    downloadableHistoryIds.every((id) => selectedDownloadIds.includes(id))

  const totalPages = historyTotal
    ? Math.max(1, Math.ceil(historyTotal / pageSize))
    : 1
  const canPrev = page > 1
  const canNext = page < totalPages

  const currentStatusType = useMemo(
    () => statusBadgeType(task?.state),
    [task?.state],
  )

  useEffect(() => {
    const allowed = new Set(downloadableHistoryIds)
    setSelectedDownloadTaskIds((prev) => {
      const next = prev.filter((id) => allowed.has(Number(id)))
      if (next.length === prev.length && next.every((id, idx) => id === prev[idx])) {
        return prev
      }
      return next
    })
  }, [downloadableHistoryIds])

  // -------- 上传文件相关 --------
  function updateBandianwaDraft(id, patch) {
    setBandianwaDrafts((list) =>
      list.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    )
  }

  function clearRegenerateTarget() {
    setRegenerateTarget(null)
  }

  function fromBandianwaAspectRatio(value) {
    if (value === '16:9') return 'landscape'
    if (value === '1:1') return 'square'
    return 'portrait'
  }

  function buildBandianwaItem(item) {
    const localImages = item.files || []

    return {
      model: modelId,
      prompt: (item.prompt || '').trim().slice(0, MAX_PROMPT_LEN),
      aspect_ratio: toBandianwaAspectRatio(item.aspectRatio),
      generate_audio: true,
      reference_images: [],
      reference_file_paths: item.serverReferenceFilePaths || [],
      reference_file_count: localImages.length,
      service_provider: 'auto',
      seconds: item.seconds,
      submit_path: currentModel.submitPath,
    }
  }

  function addBandianwaDraft(seed = null) {
    setBandianwaDrafts((list) => [
      ...list,
      createBandianwaDraft(
        seed || {
          aspectRatio: list[list.length - 1]?.aspectRatio || 'portrait',
          seconds: list[list.length - 1]?.seconds || 8,
        },
      ),
    ])
  }

  function duplicateBandianwaDraft(item) {
    addBandianwaDraft({
      prompt: item.prompt,
      aspectRatio: item.aspectRatio,
      seconds: item.seconds,
      files: (item.files || []).map(cloneDraftImage),
      serverReferenceFilePaths: [...(item.serverReferenceFilePaths || [])],
      serverReferenceFiles: (item.serverReferenceFiles || []).map((img) => ({
        ...img,
        id: `server-img-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      })),
    })
  }

  function removeBandianwaDraft(id) {
    setBandianwaDrafts((list) =>
      list.length <= 1 ? list : list.filter((item) => item.id !== id),
    )
  }

  function addBandianwaFiles(id, fileList) {
    const incoming = Array.from(fileList || []).filter((f) =>
      String(f.type || '').startsWith('image/'),
    )
    if (!incoming.length) {
      showErrorToast('请拖入图片文件')
      return
    }

    setBandianwaDrafts((list) =>
      list.map((item) => {
        if (item.id !== id) return item
        const serverReferenceCount = Math.max(
          (item.serverReferenceFiles || []).length,
          (item.serverReferenceFilePaths || []).length,
        )
        const existingCount = (item.files || []).length + serverReferenceCount
        const capacity = Math.max(0, referenceImageLimit - existingCount)
        const selected = incoming.slice(0, capacity)
        if (incoming.length > capacity) {
          showErrorToast(`每条任务最多 ${referenceImageLimit} 张参考图`)
        }
        return {
          ...item,
          files: [...item.files, ...selected.map(makeImagePreview)],
        }
      }),
    )
  }

  function removeBandianwaFile(itemId, imageId) {
    setBandianwaDrafts((list) =>
      list.map((item) =>
        item.id === itemId
          ? {
              ...item,
              files: item.files.filter((img) => img.id !== imageId),
            }
          : item,
      ),
    )
  }

  function removeBandianwaServerReferenceFile(itemId, imageId) {
    setBandianwaDrafts((list) =>
      list.map((item) => {
        if (item.id !== itemId) return item
        const removed = (item.serverReferenceFiles || []).find((img) => img.id === imageId)
        return {
          ...item,
          serverReferenceFiles: (item.serverReferenceFiles || []).filter((img) => img.id !== imageId),
          serverReferenceFilePaths: (item.serverReferenceFilePaths || []).filter((ref) => {
            const path = typeof ref === 'string' ? ref : ref?.path
            return !removed?.path || path !== removed.path
          }),
        }
      }),
    )
  }

  async function submitSingleBandianwaDraft(item) {
    if (!wid || !item?.id) return
    if (regenerateTarget) {
      showErrorToast('当前正在编辑历史任务，请先取消编辑后再单独开始')
      return
    }
    if (!isModelAvailable(modelId)) {
      showErrorToast('当前模型暂无启用且具备 Scope 的 API Key')
      return
    }

    const draft = {
      ...item,
      prompt: (item.prompt || '').trim(),
    }
    if (!draft.prompt) {
      showErrorToast(`任务 ${bandianwaDrafts.findIndex((d) => d.id === item.id) + 1} 还没有填写提示词`)
      return
    }
    const localImages = draft.files || []
    const payloadItem = buildBandianwaItem(draft)

    setSingleSubmittingDraftId(item.id)
    setErr('')
    try {
      const resp = localImages.length
        ? await aiVideoApi.createBatchUpload(wid, {
            items: [payloadItem],
            files: localImages.map((img) => img.file),
          })
        : await aiVideoApi.createBatch(wid, { items: [payloadItem] })

      const tasks = resp?.tasks || (resp?.task ? [resp.task] : [])
      const newTask = tasks[tasks.length - 1]
      if (newTask?.id) {
        setCurrentTaskId(newTask.id)
        if (typeof window !== 'undefined') {
          window.localStorage.setItem(lastTaskKey, String(newTask.id))
        }
      }
      setPage(1)
      await refetchHistory()
      showSuccessToast('该条任务已加入队列')
    } catch (e2) {
      // eslint-disable-next-line no-console
      console.error(e2)
      const msg = e2?.response?.data?.detail || e2?.message || '创建单条任务失败'
      setErr(msg)
      showErrorToast(msg)
    } finally {
      setSingleSubmittingDraftId(null)
    }
  }

  // -------- 创建任务 --------
  async function onSubmit(e) {
    e.preventDefault()
    if (!wid) {
      showErrorToast('缺少 workspace id')
      return
    }
    if (!isModelAvailable(modelId)) {
      showErrorToast('当前模型暂无启用且具备 Scope 的 API Key')
      return
    }
    const drafts = bandianwaDrafts
      .map((item) => ({ ...item, prompt: (item.prompt || '').trim() }))
      .filter((item) => item.prompt)
    if (!drafts.length) {
      showErrorToast('请至少填写一条任务提示词')
      return
    }

    const uploadFiles = []
    const items = drafts.map((item) => {
      const localImages = item.files || []
      uploadFiles.push(...localImages.map((image) => image.file))
      return buildBandianwaItem(item)
    })

    setSubmitting(true)
    setErr('')
    try {
      let response
      if (regenerateTarget) {
        if (uploadFiles.length) {
          throw new Error('编辑再生成会沿用服务器已保存的参考图；如需替换上传图片，请新建任务')
        }
        const targetIds = regenerateTarget.taskIds?.length
          ? regenerateTarget.taskIds
          : [regenerateTarget.id]
        if (items.length !== targetIds.length) {
          throw new Error('批量编辑需要保留和原批次相同的任务数量')
        }
        const tasks = []
        for (let index = 0; index < items.length; index += 1) {
          const itemResponse = await aiVideoApi.retryTask(wid, targetIds[index], {
            input_params: items[index],
          })
          tasks.push(itemResponse?.task || itemResponse)
        }
        response = { tasks }
      } else {
        response = uploadFiles.length
          ? await aiVideoApi.createBatchUpload(wid, { items, files: uploadFiles })
          : await aiVideoApi.createBatch(wid, { items })
      }

      const tasks = response?.tasks || (response?.task ? [response.task] : [])
      const lastTask = tasks[tasks.length - 1]
      if (lastTask?.id) {
        setCurrentTaskId(lastTask.id)
        window.localStorage.setItem(lastTaskKey, String(lastTask.id))
      }
      setPage(1)
      await refetchHistory()
      if (regenerateTarget) clearRegenerateTarget()
      showSuccessToast(
        regenerateTarget
          ? `任务已重新生成：${tasks.length || items.length} 条`
          : `${currentModel.label} 任务已加入队列：${tasks.length} 条`,
      )
    } catch (error) {
      console.error(error)
      const detail = error?.response?.data?.detail || error?.message || '创建视频任务失败'
      setErr(detail)
      showErrorToast(detail)
    } finally {
      setSubmitting(false)
    }
  }

  async function onRefreshTask() {
    if (!currentTaskId) return
    await refetchTask()
    await refetchHistory()
    await refetchFiles()
  }

  // 清除当前任务（不动历史）
  function onClearTask() {
    setCurrentTaskId(null)
    setPreview(null)
    if (typeof window !== 'undefined') {
      window.localStorage.removeItem(lastTaskKey)
    }
    queryClient.removeQueries({ queryKey: ['ai-video-task', wid, modelId] })
    queryClient.removeQueries({ queryKey: ['ai-video-files', wid, modelId] })
  }

  // 清空任务历史（会删除数据库记录）
  async function onClearHistory() {
    if (!wid || !historyTotal) return
    setConfirmState({
      open: true,
      title: '清空任务记录',
      content: '确定要清空当前模型的任务记录吗？此操作会删除数据库中的任务及关联文件记录，且不可恢复。',
      onOk: async () => {
        try {
          await aiVideoApi.clearTasks(wid, { modelId })

          // 清理前端状态
          setCurrentTaskId(null)
          setPreview(null)
          if (typeof window !== 'undefined') {
            window.localStorage.removeItem(lastTaskKey)
          }
          queryClient.removeQueries({ queryKey: ['ai-video-task', wid, modelId] })
          queryClient.removeQueries({ queryKey: ['ai-video-files', wid, modelId] })

          setPage(1)
          await refetchHistory()
          showSuccessToast('任务记录已清空')
        } catch (e2) {
          // eslint-disable-next-line no-console
          console.error(e2)
          showErrorToast(e2?.message || '清空任务记录失败')
        }
      },
      onCancel: () => setConfirmState({ open: false }),
    })
  }

  async function onRetryTaskItem(taskItem) {
    if (!wid || !taskItem?.id) return
    try {
      const resp = await aiVideoApi.retryTask(wid, taskItem.id)
      const nextTask = resp?.task || resp
      if (nextTask?.id) {
        setCurrentTaskId(nextTask.id)
        if (typeof window !== 'undefined') {
          window.localStorage.setItem(lastTaskKey, String(nextTask.id))
        }
      }
      await refetchHistory()
      await refetchTask()
      showSuccessToast(
        taskItem?.fail_code === 'local_download_failed'
          ? '结果文件已重新加入下载队列'
          : canRetryTask(taskItem)
            ? '任务已重新加入队列'
            : '任务已重新生成',
      )
    } catch (e2) {
      // eslint-disable-next-line no-console
      console.error(e2)
      showErrorToast(e2?.message || '重试任务失败')
    }
  }

  async function loadBandianwaServerReferenceFiles(taskItem, input) {
    const referencePaths = Array.isArray(input.reference_file_paths)
      ? input.reference_file_paths
      : []
    let loadedFiles = []
    if (wid && taskItem?.id) {
      try {
        const taskFiles = await aiVideoApi.listTaskFiles(wid, taskItem.id)
        const referenceFiles = (taskFiles || []).filter((file) => file.kind === 'reference_upload')
        loadedFiles = await Promise.all(
          referenceFiles.map(async (file, index) => {
            let previewUrl = ''
            try {
              previewUrl = await aiVideoApi.getFileDownloadUrl(wid, file.id)
            } catch {
              previewUrl = ''
            }
            return {
              id: `server-img-${file.id || index}-${Date.now()}`,
              fileId: file.id,
              path: file.file_url,
              filename: file.file_url ? file.file_url.split('/').pop() : `参考图 ${index + 1}`,
              previewUrl,
            }
          }),
        )
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn('load Bandianwa reference files failed', error)
      }
    }
    const paths = referencePaths.length
      ? referencePaths
      : loadedFiles.map((file) => ({ path: file.path, filename: file.filename }))
    return normalizeServerReferenceFiles(paths, loadedFiles)
  }

  async function buildBandianwaDraftFromTask(taskItem) {
    const input = taskItem.input_json || {}
    const serverReferenceFiles = await loadBandianwaServerReferenceFiles(taskItem, input)
    return createBandianwaDraft({
      prompt: input.prompt || taskItem.prompt || '',
      aspectRatio: fromBandianwaAspectRatio(input.aspect_ratio),
      seconds: Number(input.seconds || input.duration) || 8,
      files: [],
      serverReferenceFilePaths: input.reference_file_paths || [],
      serverReferenceFiles,
    })
  }

  async function onEditRegenerateTaskItem(taskItem) {
    if (!taskItem?.id) return
    skipNextModelResetRef.current = true
    setModelId(normalizeModelId(taskItem.model || modelId))
    setCurrentTaskId(taskItem.id)
    window.localStorage.setItem(lastTaskKey, String(taskItem.id))

    let batchTasks = [taskItem]
    try {
      batchTasks = await aiVideoApi.getTaskBatch(wid, taskItem.id)
    } catch (error) {
      console.warn('load AI video task batch failed', error)
    }
    const orderedTasks = (batchTasks || [taskItem])
      .filter(Boolean)
      .sort((left, right) => {
        const leftIndex = Number(left.batch_index || 999999)
        const rightIndex = Number(right.batch_index || 999999)
        return leftIndex !== rightIndex ? leftIndex - rightIndex : Number(left.id) - Number(right.id)
      })
    const drafts = await Promise.all(orderedTasks.map(buildBandianwaDraftFromTask))
    setBandianwaDrafts(drafts.length ? drafts : [await buildBandianwaDraftFromTask(taskItem)])
    setRegenerateTarget({
      id: taskItem.id,
      taskIds: orderedTasks.map((item) => item.id),
      batchSize: orderedTasks.length,
    })
    showSuccessToast(
      orderedTasks.length > 1
        ? '已载入整批任务，修改后点击重新生成'
        : '已载入该条任务，修改后点击重新生成',
    )
  }

  async function onDeleteTaskItem(taskItem) {
    if (!wid || !taskItem?.id) return
    setConfirmState({
      open: true,
      title: '删除任务',
      content: `确定删除任务 #${taskItem.id} 吗？对应的本地结果文件也会一起清理。`,
      onOk: async () => {
        try {
          await aiVideoApi.deleteTask(wid, taskItem.id)
          if (Number(currentTaskId) === Number(taskItem.id)) {
            onClearTask()
          }
          await refetchHistory()
          showSuccessToast('任务已删除')
        } catch (e2) {
          // eslint-disable-next-line no-console
          console.error(e2)
          showErrorToast(e2?.message || '删除任务失败')
        }
      },
      onCancel: () => setConfirmState({ open: false }),
    })
  }

  // 下载视频
  async function handleDownload(fileId) {
    if (!wid || !fileId) return
    try {
      const url = await aiVideoApi.getFileDownloadUrl(wid, fileId)
      if (!url) return
      const a = document.createElement('a')
      a.href = url
      a.target = '_blank'
      a.rel = 'noopener'
      a.download = ''
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
    } catch (e2) {
      // eslint-disable-next-line no-console
      console.error(e2)
      showErrorToast(e2?.message || '获取下载链接失败')
    }
  }

  function toggleDownloadSelection(taskItem) {
    if (!canBatchDownloadTask(taskItem)) return
    const taskId = Number(taskItem.id)
    setSelectedDownloadTaskIds((prev) =>
      prev.includes(taskId)
        ? prev.filter((id) => id !== taskId)
        : [...prev, taskId],
    )
  }

  function toggleSelectAllDownloadable() {
    if (!downloadableHistoryIds.length) return
    setSelectedDownloadTaskIds((prev) => {
      const selected = prev.filter((id) => downloadableHistoryIds.includes(Number(id)))
      if (selected.length === downloadableHistoryIds.length) {
        return prev.filter((id) => !downloadableHistoryIds.includes(Number(id)))
      }
      const merged = new Set([...prev, ...downloadableHistoryIds])
      return Array.from(merged)
    })
  }

  async function handleBatchDownload() {
    if (!wid || !selectedDownloadIds.length || batchDownloading) return
    setBatchDownloading(true)
    try {
      const result = await aiVideoApi.batchDownloadTasks(wid, selectedDownloadIds)
      if (!result?.blob) {
        showErrorToast('没有可下载的视频文件')
        return
      }
      const blobUrl = URL.createObjectURL(result.blob)
      const a = document.createElement('a')
      a.href = blobUrl
      a.download = result.filename || 'ai-videos.zip'
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(blobUrl), 1000)
      showSuccessToast(`已打包 ${selectedDownloadIds.length} 个任务`)
    } catch (e2) {
      // eslint-disable-next-line no-console
      console.error(e2)
      showErrorToast(e2?.message || '批量下载失败')
    } finally {
      setBatchDownloading(false)
    }
  }

  // 预览视频
  async function handlePreview(fileObj) {
    if (!wid || !fileObj?.id) return
    try {
      const url = await aiVideoApi.getFileDownloadUrl(wid, fileObj.id)
      if (!url) return
      setPreview({
        url,
        kind: fileObj.kind,
        mime: fileObj.mime_type || '',
      })
    } catch (e2) {
      // eslint-disable-next-line no-console
      console.error(e2)
      showErrorToast(e2?.message || '获取预览链接失败')
    }
  }

  function closePreview() {
    setPreview(null)
  }

  const pageTitle = '生成视频'
  const pageIntro = `${currentModel.label} · 支持批量任务、参考图预览和后台异步生成。系统按模型优先级自动选择可用 API。`

  return (
    <div className="card card--elevated">
      <h2 style={{ marginTop: 0 }}>{pageTitle}</h2>

      {/* 模型切换 Tab */}
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 8,
          marginBottom: 12,
        }}
      >
        {MODEL_CONFIGS.map((m) => (
          <button
            key={m.id}
            type="button"
            onClick={() => isModelAvailable(m.id) && setModelId(m.id)}
            disabled={!isModelAvailable(m.id)}
            title={!isModelAvailable(m.id) ? '后台尚未配置此模型的可用 API Key' : ''}
            className={modelId === m.id ? 'btn sm' : 'btn ghost sm'}
            style={{ fontSize: 13, opacity: isModelAvailable(m.id) ? 1 : 0.45 }}
          >
            {m.label}
          </button>
        ))}
      </div>

      <p className="small-muted" style={{ marginBottom: 16 }}>
        {pageIntro}
      </p>
      {!isModelAvailable(modelId) && (
        <div className="alert alert--error" style={{ marginBottom: 16 }}>
          当前模型暂无可用 API Key，请在平台“API Key 管理”中启用对应 Scope。
        </div>
      )}

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 2fr) minmax(0, 3fr)',
          gap: 24,
          alignItems: 'flex-start',
        }}
      >
        {/* 左侧：创建任务 */}
        <form onSubmit={onSubmit} className="form-grid" style={{ marginBottom: 24 }}>
          {/* 提示词 */}





            <div style={{ gridColumn: '1 / -1' }}>
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  gap: 12,
                  marginBottom: 12,
                }}
              >
                <div>
                  <strong>批量任务输入口</strong>
                  <div className="small-muted" style={{ marginTop: 4 }}>
                    每条任务独立设置提示词、画幅、时长和参考图。
                  </div>
                </div>
                <button
                  type="button"
                  className="btn"
                  onClick={() => addBandianwaDraft()}
                >
                  + 新增任务
                </button>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                {bandianwaDrafts.map((item, index) => (
                  <div
                    key={item.id}
                    style={{
                      border: '1px solid #dbeafe',
                      borderRadius: 12,
                      padding: 14,
                      background: '#f8fbff',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        gap: 8,
                        marginBottom: 10,
                      }}
                    >
                      <strong>任务 {index + 1}</strong>
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                        <button
                          type="button"
                          className="btn sm"
                          disabled={submitting || singleSubmittingDraftId === item.id}
                          onClick={() => submitSingleBandianwaDraft(item)}
                        >
                          {singleSubmittingDraftId === item.id ? '开始中…' : '开始'}
                        </button>
                        <button
                          type="button"
                          className="btn ghost sm"
                          disabled={singleSubmittingDraftId === item.id}
                          onClick={() => duplicateBandianwaDraft(item)}
                        >
                          复制
                        </button>
                        <button
                          type="button"
                          className="btn ghost sm"
                          disabled={bandianwaDrafts.length <= 1 || singleSubmittingDraftId === item.id}
                          onClick={() => removeBandianwaDraft(item.id)}
                        >
                          删除
                        </button>
                      </div>
                    </div>

                    <FormField label="提示词">
                      <textarea
                        rows={4}
                        value={item.prompt}
                        onChange={(e) =>
                          updateBandianwaDraft(item.id, {
                            prompt: e.target.value,
                          })
                        }
                        maxLength={MAX_PROMPT_LEN}
                        placeholder="请描述这条视频要生成的内容、人物、动作、镜头、风格..."
                        style={{
                          width: '100%',
                          resize: 'vertical',
                          borderRadius: 8,
                        }}
                      />
                    </FormField>

                    <div
                      style={{
                        display: 'flex',
                        gap: 12,
                        flexWrap: 'wrap',
                        alignItems: 'flex-start',
                      }}
                    >
                      <FormField label="画面比例">
                        <div style={{ display: 'inline-flex', gap: 8 }}>
                          {aspectOptions.map((opt) => (
                            <button
                              key={opt.value}
                              type="button"
                              onClick={() =>
                                updateBandianwaDraft(item.id, {
                                  aspectRatio: opt.value,
                                })
                              }
                              className={item.aspectRatio === opt.value ? 'btn' : 'btn ghost'}
                              style={{ padding: '6px 14px', fontSize: 13 }}
                            >
                              {opt.label}
                            </button>
                          ))}
                        </div>
                      </FormField>

                      <FormField label="视频时长">
                        <div style={{ display: 'inline-flex', gap: 8, flexWrap: 'wrap' }}>
                            {(currentModel.kind === 'volcengine-seedance-mini' ? SEEDANCE_SECONDS_OPTIONS : BANDIANWA_SECONDS_OPTIONS).map((opt) => (
                              <button
                                key={opt.value}
                                type="button"
                                onClick={() =>
                                  updateBandianwaDraft(item.id, {
                                    seconds: opt.value,
                                  })
                                }
                                className={
                                  item.seconds === opt.value ? 'btn' : 'btn ghost'
                                }
                                style={{ padding: '6px 14px', fontSize: 13 }}
                              >
                                {opt.label}
                              </button>
                            ))}
                          </div>
                      </FormField>

                    </div>

                    <FormField label={`参考图（拖拽或点击上传，最多 ${referenceImageLimit} 张）`}>
                        <div
                          onDragOver={(e) => {
                            e.preventDefault()
                          }}
                          onDrop={(e) => {
                            e.preventDefault()
                            addBandianwaFiles(item.id, e.dataTransfer.files)
                          }}
                          style={{
                            border: '2px dashed #bfdbfe',
                            borderRadius: 12,
                            padding: 14,
                            background: '#fff',
                          }}
                        >
                          <label
                            style={{
                              display: 'block',
                              cursor: 'pointer',
                              color: '#2563eb',
                              fontSize: 13,
                              marginBottom: 10,
                            }}
                          >
                            拖拽图片到这里，或点击选择图片
                            <input
                              type="file"
                              accept="image/*"
                              multiple
                              style={{ display: 'none' }}
                              onChange={(e) => {
                                addBandianwaFiles(item.id, e.target.files)
                                e.target.value = ''
                              }}
                            />
                          </label>

                          {(item.serverReferenceFiles || []).length + item.files.length > 0 ? (
                            <div
                              style={{
                                display: 'grid',
                                gridTemplateColumns:
                                  'repeat(auto-fill, minmax(86px, 1fr))',
                                gap: 10,
                              }}
                            >
                              {(item.serverReferenceFiles || []).map((img, imgIndex) => (
                                <div
                                  key={img.id}
                                  style={{
                                    position: 'relative',
                                    border: '1px solid #bfdbfe',
                                    borderRadius: 10,
                                    overflow: 'hidden',
                                    background: '#eff6ff',
                                  }}
                                >
                                  {img.previewUrl ? (
                                    <img
                                      src={img.previewUrl}
                                      alt={`服务器参考图 ${imgIndex + 1}`}
                                      style={{
                                        width: '100%',
                                        height: 86,
                                        objectFit: 'cover',
                                        display: 'block',
                                      }}
                                    />
                                  ) : (
                                    <div
                                      className="small-muted"
                                      style={{
                                        height: 86,
                                        display: 'flex',
                                        alignItems: 'center',
                                        justifyContent: 'center',
                                        padding: 8,
                                        textAlign: 'center',
                                      }}
                                    >
                                      已保存参考图
                                    </div>
                                  )}
                                  <span
                                    style={{
                                      position: 'absolute',
                                      left: 6,
                                      top: 6,
                                      background: '#2563eb',
                                      color: '#fff',
                                      borderRadius: 999,
                                      fontSize: 11,
                                      padding: '1px 6px',
                                    }}
                                  >
                                    {imgIndex + 1}
                                  </span>
                                  <button
                                    type="button"
                                    className="btn danger sm"
                                    onClick={() =>
                                      removeBandianwaServerReferenceFile(item.id, img.id)
                                    }
                                    style={{
                                      position: 'absolute',
                                      right: 4,
                                      top: 4,
                                      padding: '2px 6px',
                                      fontSize: 11,
                                    }}
                                  >
                                    ×
                                  </button>
                                </div>
                              ))}
                              {item.files.map((img, imgIndex) => (
                                <div
                                  key={img.id}
                                  style={{
                                    position: 'relative',
                                    border: '1px solid #e5e7eb',
                                    borderRadius: 10,
                                    overflow: 'hidden',
                                    background: '#f9fafb',
                                  }}
                                >
                                  <img
                                    src={img.previewUrl}
                                    alt={`参考图 ${imgIndex + 1}`}
                                    style={{
                                      width: '100%',
                                      height: 86,
                                      objectFit: 'cover',
                                      display: 'block',
                                    }}
                                  />
                                  <span
                                    style={{
                                      position: 'absolute',
                                      left: 6,
                                      top: 6,
                                      background: '#2563eb',
                                      color: '#fff',
                                      borderRadius: 999,
                                      fontSize: 11,
                                      padding: '1px 6px',
                                    }}
                                  >
                                    {(item.serverReferenceFiles || []).length + imgIndex + 1}
                                  </span>
                                  <button
                                    type="button"
                                    className="btn danger sm"
                                    onClick={() =>
                                      removeBandianwaFile(item.id, img.id)
                                    }
                                    style={{
                                      position: 'absolute',
                                      right: 4,
                                      top: 4,
                                      padding: '2px 6px',
                                      fontSize: 11,
                                    }}
                                  >
                                    ×
                                  </button>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div className="small-muted">
                              不上传图片时会按文生视频提交。
                            </div>
                          )}
                        </div>
                    </FormField>
                  </div>
                ))}
              </div>
            </div>


          <div style={{ marginTop: 8 }}>
            {regenerateTarget && (
              <div
                style={{
                  marginBottom: 10,
                  padding: '8px 10px',
                  borderRadius: 8,
                  background: '#eff6ff',
                  border: '1px solid #bfdbfe',
                  color: '#1d4ed8',
                  fontSize: 13,
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 10,
                  alignItems: 'center',
                }}
              >
                <span>正在编辑任务 #{regenerateTarget.id}，提交后会在同一条记录下重新生成。</span>
                <button type="button" className="btn ghost sm" onClick={clearRegenerateTarget}>
                  取消编辑
                </button>
              </div>
            )}
            <button className="btn" type="submit" disabled={submitting}>
              {submitting
                ? regenerateTarget
                  ? '重新生成中…'
                  : '创建中…'
                : regenerateTarget
                  ? '重新生成当前任务'
                  : '创建任务'}
            </button>
          </div>
        </form>

        {/* 右侧：当前任务 + 历史 */}
        <div>
          {/* 错误提示 */}
          {(err || taskError) && (
            <div className="alert alert--error" style={{ marginBottom: 12 }}>
              {err || taskError?.message || '请求失败'}
            </div>
          )}

          {/* 当前任务状态 */}
          <section style={{ marginBottom: 20 }}>
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: 8,
              }}
            >
              <h3 style={{ margin: 0 }}>当前任务</h3>
              <div style={{ display: 'flex', gap: 8 }}>
                {task?.id && (
                  <>
                    <button
                      className="btn ghost"
                      type="button"
                      onClick={onRefreshTask}
                    >
                      刷新状态
                    </button>
                    <button
                      className="btn ghost"
                      type="button"
                      onClick={onClearTask}
                    >
                      清除当前任务
                    </button>
                    {canRegenerateTask(task) && (
                      <button
                        className="btn ghost"
                        type="button"
                        onClick={() => onRetryTaskItem(task)}
                      >
                        {canRetryTask(task) ? '重试任务' : '重新生成'}
                      </button>
                    )}
                    {canRegenerateTask(task) && (
                      <button
                        className="btn ghost"
                        type="button"
                        onClick={() => onEditRegenerateTaskItem(task)}
                      >
                        编辑再生成
                      </button>
                    )}
                    {canDeleteTask(task) && (
                      <button
                        className="btn ghost"
                        type="button"
                        onClick={() => onDeleteTaskItem(task)}
                      >
                        删除任务
                      </button>
                    )}
                  </>
                )}
              </div>
            </div>

            {!task && !loadingTask && (
              <div className="small-muted">
                暂无任务。创建一个任务后，这里会显示最近一次任务状态。
              </div>
            )}

            {loadingTask && <Loading />}

            {task && !loadingTask && (
              <div className="card" style={{ marginTop: 4 }}>
                <div style={{ marginBottom: 8 }}>
                  <div>
                    <strong>任务 ID：</strong>
                    {task.id}
                  </div>
                  <div>
                    <strong>模型：</strong>
                    {task.model}
                  </div>
                  <div>
                    <strong>创建人：</strong>
                    {formatTaskCreator(task)}
                  </div>
                  <div
                    style={{
                      marginTop: 4,
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                    }}
                  >
                    <strong>状态：</strong>
                    <Badge type={currentStatusType}>
                      {(task.state || '').toString()}
                    </Badge>
                  </div>

                  {/* 简单进度条 */}
                  <div
                    style={{
                      marginTop: 8,
                      height: 6,
                      borderRadius: 999,
                      background: '#e5e7eb',
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        width:
                          currentStatusType === 'waiting'
                            ? '20%'
                            : currentStatusType === 'running'
                              ? '60%'
                              : '100%',
                        background:
                          currentStatusType === 'success'
                            ? '#16a34a'
                            : currentStatusType === 'fail' || currentStatusType === 'timeout'
                              ? '#dc2626'
                              : '#0d6efd',
                        height: '100%',
                        transition: 'width 0.3s ease',
                      }}
                    />
                  </div>

                  {(task.fail_code || task.fail_msg) && (
                    <div
                      style={{
                        marginTop: 8,
                        padding: '6px 8px',
                        borderRadius: 6,
                        backgroundColor: '#fef2f2',
                        color: '#b91c1c',
                        fontSize: 12,
                      }}
                    >
                      <strong>失败原因：</strong>
                      {task.fail_code && <>[{task.fail_code}] </>}
                      {task.fail_msg || '未知错误'}
                    </div>
                  )}
                </div>

                {task.prompt && (
                  <details style={{ marginTop: 4 }}>
                    <summary className="small-muted">查看提示信息</summary>
                    <pre
                      style={{
                        marginTop: 4,
                        maxHeight: 200,
                        overflow: 'auto',
                        whiteSpace: 'pre-wrap',
                        fontSize: 12,
                        background: '#f7f7f7',
                        padding: 8,
                        borderRadius: 4,
                      }}
                    >
                      {task.prompt}
                    </pre>
                  </details>
                )}

                <div style={{ marginTop: 12 }}>
                  <strong>视频生成：</strong>
                  {files && files.length > 0 ? (
                    <ul style={{ marginTop: 6, paddingLeft: 18 }}>
                      {files
                        .filter((f) => f.kind === 'result')
                        .map((f) => (
                          <li key={f.id} style={{ marginBottom: 6 }}>
                            <div>[结果文件]</div>
                            <div
                              style={{
                                marginTop: 4,
                                display: 'flex',
                                gap: 8,
                                flexWrap: 'wrap',
                              }}
                            >
                              <button
                                type="button"
                                className="btn ghost"
                                onClick={() => handlePreview(f)}
                              >
                                预览视频
                              </button>
                              <button
                                type="button"
                                className="btn ghost"
                                onClick={() => handleDownload(f.id)}
                              >
                                下载视频
                              </button>
                              {task.fail_code === 'local_download_failed' &&
                                /^https?:\/\//i.test(f.file_url || '') && (
                                  <a
                                    className="btn ghost"
                                    href={f.file_url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    download
                                  >
                                    远端下载
                                  </a>
                                )}
                            </div>
                          </li>
                        ))}
                    </ul>
                  ) : (
                    <div className="small-muted">
                      {shouldPollByState(task.state)
                        ? '结果文件正在后台下载，请稍后刷新状态。'
                        : '暂无文件，请稍后刷新状态。'}
                    </div>
                  )}
                </div>
              </div>
            )}
          </section>

          {/* 任务历史 */}
          <section>
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: 8,
                gap: 12,
              }}
            >
              <h3 style={{ margin: 0 }}>我的任务记录</h3>
              <div
                style={{
                  display: 'flex',
                  gap: 10,
                  alignItems: 'center',
                  flexWrap: 'wrap',
                }}
              >
                <label
                  className="small-muted"
                  style={{ display: 'flex', alignItems: 'center', gap: 6 }}
                >
                  每页
                  <select
                    value={pageSize}
                    onChange={(e) =>
                      setPageSize(Number(e.target.value) || 10)
                    }
                    style={{
                      padding: '4px 8px',
                      borderRadius: 8,
                      border: '1px solid var(--border)',
                      background: 'var(--panel-2)',
                      color: 'inherit',
                      fontSize: 12,
                    }}
                  >
                    {PAGE_SIZE_OPTIONS.map((sz) => (
                      <option key={sz} value={sz}>
                        {sz}
                      </option>
                    ))}
                  </select>
                  条
                </label>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => refetchHistory()}
                  disabled={historyLoading}
                >
                  {historyLoading ? '刷新中…' : '刷新'}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={toggleSelectAllDownloadable}
                  disabled={historyLoading || !downloadableHistoryIds.length}
                >
                  {allDownloadableSelected ? '取消选择' : '选择本页成功'}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={handleBatchDownload}
                  disabled={historyLoading || batchDownloading || !selectedDownloadIds.length}
                >
                  {batchDownloading ? '打包中…' : `批量下载${selectedDownloadIds.length ? `(${selectedDownloadIds.length})` : ''}`}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={onClearHistory}
                  disabled={historyLoading || !historyTotal}
                >
                  清空记录
                </button>
              </div>
            </div>

            {historyLoading && <Loading />}

            {!historyLoading && history.length === 0 && (
              <div className="small-muted">暂无历史记录。</div>
            )}

            {!historyLoading && history.length > 0 && (
              <>
                <div
                  className="table-wrapper"
                  style={{ maxHeight: 340, overflow: 'auto' }}
                >
                  <table className="table" style={{ minWidth: 880, tableLayout: 'fixed' }}>
                    <thead>
                      <tr>
                        <th style={{ width: 44 }}>
                          <input
                            type="checkbox"
                            checked={allDownloadableSelected}
                            disabled={!downloadableHistoryIds.length}
                            onChange={toggleSelectAllDownloadable}
                            aria-label="选择本页成功任务"
                          />
                        </th>
                        <th style={{ width: 70 }}>ID</th>
                        <th style={{ width: 92 }}>状态</th>
                        <th style={{ width: 150 }}>创建时间</th>
                        <th style={{ width: 260 }}>操作</th>
                        <th>提示摘要 / 链接</th>
                      </tr>
                    </thead>
                    <tbody>
                      {history.map((t) => (
                        <tr key={t.id} style={{ height: 54 }}>
                          <td>
                            <input
                              type="checkbox"
                              checked={selectedDownloadIds.includes(Number(t.id))}
                              disabled={!canBatchDownloadTask(t)}
                              onChange={() => toggleDownloadSelection(t)}
                              aria-label={`选择任务 ${t.id}`}
                            />
                          </td>
                          <td>{t.id}</td>
                          <td>
                            <Badge type={statusBadgeType(t.state)}>
                              {t.state}
                            </Badge>
                          </td>
                          <td className="small-muted" style={{ whiteSpace: 'nowrap' }}>
                            {t.created_at
                              ? new Date(t.created_at).toLocaleString()
                              : ''}
                          </td>
                          <td>
                            <div
                              style={{
                                display: 'flex',
                                gap: 6,
                                flexWrap: 'nowrap',
                                alignItems: 'center',
                                whiteSpace: 'nowrap',
                              }}
                            >
                              <button
                                type="button"
                                className="btn ghost sm"
                                onClick={() => {
                                  setCurrentTaskId(t.id)
                                  if (typeof window !== 'undefined') {
                                    window.localStorage.setItem(
                                      lastTaskKey,
                                      String(t.id),
                                    )
                                  }
                                }}
                              >
                                查看
                              </button>
                              {canRegenerateTask(t) && (
                                <button
                                  type="button"
                                  className="btn ghost sm"
                                  onClick={() => onRetryTaskItem(t)}
                                >
                                  {canRetryTask(t) ? '重试' : '重新生成'}
                                </button>
                              )}
                              {canRegenerateTask(t) && (
                                <button
                                  type="button"
                                  className="btn ghost sm"
                                  onClick={() => onEditRegenerateTaskItem(t)}
                                >
                                  编辑
                                </button>
                              )}
                              {canDeleteTask(t) && (
                                <button
                                  type="button"
                                  className="btn ghost sm"
                                  onClick={() => onDeleteTaskItem(t)}
                                >
                                  删除
                                </button>
                              )}
                            </div>
                          </td>
                          <td
                            className="small-muted"
                            title={t.prompt || ''}
                            style={{
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {t.prompt || ''}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div
                  className="small-muted"
                  style={{
                    marginTop: 6,
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    flexWrap: 'wrap',
                    gap: 8,
                  }}
                >
                  <span>
                    共 {historyTotal} 条记录（最多展示 {MAX_HISTORY_TOTAL} 条）
                  </span>
                  <div
                    style={{ display: 'flex', alignItems: 'center', gap: 8 }}
                  >
                    <button
                      type="button"
                      className="btn ghost sm"
                      disabled={!canPrev}
                      onClick={() => canPrev && setPage((p) => p - 1)}
                    >
                      上一页
                    </button>
                    <span>
                      第 {page} / {totalPages} 页
                    </span>
                    <button
                      type="button"
                      className="btn ghost sm"
                      disabled={!canNext}
                      onClick={() => canNext && setPage((p) => p + 1)}
                    >
                      下一页
                    </button>
                  </div>
                </div>
              </>
            )}
          </section>

          <p className="small-muted" style={{ marginTop: 16 }}>
            提示：视频实际画质、时长、转场效果由模型与平台控制，当前不做内容审核。建议在提示词或分镜中描述完整剧本，并合理设置画幅比例与时长。
          </p>
        </div>
      </div>

      {/* 预览弹窗：视频小卡片 */}
      {preview && (
        <div className="modal-backdrop" onClick={closePreview}>
          <div
            className="modal"
            onClick={(e) => e.stopPropagation()}
            style={{ width: 'min(820px, 92vw)' }}
          >
            <div className="modal__header">
              <div className="modal__title">视频预览</div>
              <button
                className="modal__close"
                type="button"
                onClick={closePreview}
              >
                关闭
              </button>
            </div>
            <div className="modal__body" style={{ textAlign: 'center' }}>
              <video
                src={preview.url}
                controls
                autoPlay
                style={{
                  maxWidth: '100%',
                  maxHeight: '72vh',
                  borderRadius: 12,
                  boxShadow: '0 8px 30px rgba(0,0,0,.25)',
                }}
              />
            </div>
          </div>
        </div>
      )}

      {confirmState.open && (
        <ConfirmDialog
          open={confirmState.open}
          title={confirmState.title}
          content={confirmState.content}
          onCancel={() => setConfirmState({ open: false })}
          onOk={confirmState.onOk}
        />
      )}
    </div>
  )
}
