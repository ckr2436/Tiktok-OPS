// src/features/tenants/kie_ai/pages/Sora2ImageToVideoPage.jsx
import { useEffect, useState, useMemo } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import FormField from '../../../../components/ui/FormField.jsx'
import Loading from '../../../../components/ui/Loading.jsx'
import kieTenantApi from '../service.js'

const MAX_PROMPT_LEN = 10_000
const LAST_TASK_KEY_PREFIX = 'kie_sora2_last_task_'
const MAX_HISTORY_TOTAL = 500

const DEFAULT_MODEL_ID = 'sora-2-image-to-video'

function getLastTaskKey(wid, modelId) {
  return `${LAST_TASK_KEY_PREFIX}${wid ?? ''}_${modelId}`
}

const ASPECT_OPTIONS = [
  { value: 'portrait', label: '竖屏 9:16' },
  { value: 'landscape', label: '横屏 16:9' },
]

const DURATION_OPTIONS = [
  { value: 10, label: '10 秒' },
  { value: 15, label: '15 秒' },
]

const STORYBOARD_DURATION_OPTIONS = [
  { value: 10, label: '10 秒' },
  { value: 15, label: '15 秒' },
  { value: 25, label: '25 秒' },
]

const PAGE_SIZE_OPTIONS = [10, 20, 50]

const MODEL_CONFIGS = [
  {
    id: 'sora-2-text-to-video',
    label: 'Sora 2 Text To Video',
    kind: 'text',
    hasPrompt: true,
    hasImageUpload: false,
    hasSize: false,
    hasWatermarkToggle: true,
  },
  {
    id: 'sora-2-image-to-video',
    label: 'Sora 2 Image To Video',
    kind: 'image',
    hasPrompt: true,
    hasImageUpload: true,
    hasSize: false,
    hasWatermarkToggle: true,
  },
  {
    id: 'sora-2-pro-text-to-video',
    label: 'Sora 2 Pro Text To Video',
    kind: 'text-pro',
    hasPrompt: true,
    hasImageUpload: false,
    hasSize: true,
    defaultSize: 'high',
    hasWatermarkToggle: true,
  },
  {
    id: 'sora-2-pro-image-to-video',
    label: 'Sora 2 Pro Image To Video',
    kind: 'image-pro',
    hasPrompt: true,
    hasImageUpload: true,
    hasSize: true,
    defaultSize: 'standard',
    hasWatermarkToggle: true,
  },
  {
    id: 'sora-2-pro-storyboard',
    label: 'Sora 2 Pro Storyboard',
    kind: 'storyboard',
    hasPrompt: false,
    hasImageUpload: true,
    hasSize: false,
    hasWatermarkToggle: false,
  },
  {
    id: 'sora-watermark-remover',
    label: 'Sora Watermark Remover',
    kind: 'watermark',
    hasPrompt: false,
    hasImageUpload: false,
    hasSize: false,
    hasWatermarkToggle: false,
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
    s === 'ok' ||
    s.includes('fail') ||
    s.includes('error') ||
    s.includes('timeout')
  ) {
    return false
  }

  if (
    s.includes('wait') ||
    s.includes('queue') ||
    s.includes('run') ||
    s.includes('process')
  ) {
    return true
  }

  return false
}

// 小工具：并发限制的批量执行（最多 concurrency 个 worker 同时跑）
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

export default function Sora2ImageToVideoPage() {
  const { wid } = useParams()
  const queryClient = useQueryClient()

  const [modelId, setModelId] = useState(DEFAULT_MODEL_ID)

  const currentModel = useMemo(
    () => MODEL_CONFIGS.find((m) => m.id === modelId) || MODEL_CONFIGS[0],
    [modelId],
  )

  const [prompt, setPrompt] = useState('')
  const [aspectRatio, setAspectRatio] = useState('portrait')
  const [nFrames, setNFrames] = useState(10)
  const [removeWatermark, setRemoveWatermark] = useState(true)
  const [size, setSize] = useState('standard')
  const [videoUrl, setVideoUrl] = useState('')

  // Storyboard 分镜
  const [shots, setShots] = useState([{ duration: 5, scene: '' }])

  const [file, setFile] = useState(null)
  const [filePreview, setFilePreview] = useState(null)
  const [dragOver, setDragOver] = useState(false)

  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState('')

  // 当前任务 ID
  const [currentTaskId, setCurrentTaskId] = useState(null)

  // 历史分页
  const [pageSize, setPageSize] = useState(10)
  const [page, setPage] = useState(1)

  // 预览弹窗（视频）
  const [preview, setPreview] = useState(null) // { url, kind, mime }

  const lastTaskKey = useMemo(
    () => getLastTaskKey(wid, modelId),
    [wid, modelId],
  )

  // 模型切换时重置部分状态
  useEffect(() => {
    setErr('')
    setCurrentTaskId(null)
    setPreview(null)
    setPage(1)

    if (currentModel.kind === 'watermark') {
      setPrompt('')
    }

    if (currentModel.hasSize && currentModel.defaultSize) {
      setSize(currentModel.defaultSize)
    } else {
      setSize('standard')
    }

    if (currentModel.kind === 'storyboard') {
      setShots([{ duration: 5, scene: '' }])
    }
  }, [modelId]) // eslint-disable-line react-hooks/exhaustive-deps

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

  // ------- React Query：当前任务 -------
  const {
    data: task,
    isLoading: loadingTask,
    error: taskError,
    refetch: refetchTask,
  } = useQuery({
    queryKey: ['sora2-task', wid, modelId, currentTaskId],
    queryFn: () => kieTenantApi.getTask(wid, currentTaskId, { refresh: true }),
    enabled: !!wid && !!currentTaskId,
    refetchInterval: (query) =>
      shouldPollByState(query.state.data?.state) ? 8000 : false,
  })

  // 如果后端返回 404（getTask → null），自动清理本地“当前任务”状态和缓存
  useEffect(() => {
    if (!currentTaskId) return
    if (loadingTask) return
    if (task !== null) return // null 表示 404；undefined 是还没拉到

    setCurrentTaskId(null)
    setPreview(null)

    if (typeof window !== 'undefined') {
      window.localStorage.removeItem(lastTaskKey)
    }

    queryClient.removeQueries({ queryKey: ['sora2-task', wid, modelId] })
    queryClient.removeQueries({ queryKey: ['sora2-files', wid, modelId] })
  }, [currentTaskId, loadingTask, task, lastTaskKey, wid, modelId, queryClient])

  // ------- React Query：当前任务文件 -------
  const { data: files = [] } = useQuery({
    queryKey: ['sora2-files', wid, modelId, currentTaskId],
    queryFn: () => kieTenantApi.listTaskFiles(wid, currentTaskId),
    enabled: !!wid && !!currentTaskId,
    refetchInterval: () => {
      // ✅ 没有 task 的时候不再轮询 files，避免你说的“空仍然不停调用”
      if (!task) return false
      return shouldPollByState(task.state) ? 8000 : false
    },
  })

  // ------- React Query：任务历史 -------
  const {
    data: historyResp,
    isLoading: historyLoading,
    refetch: refetchHistory,
  } = useQuery({
    queryKey: ['sora2-history', wid, modelId, page, pageSize],
    queryFn: () =>
      kieTenantApi.listTasks(wid, { page, size: pageSize, model: modelId }),
    enabled: !!wid,
    refetchInterval: false,
    keepPreviousData: true,
  })

  const rawTotal = historyResp?.total ?? 0
  const historyTotal = Math.min(rawTotal, MAX_HISTORY_TOTAL)
  const history = historyResp?.items || []

  const totalPages = historyTotal
    ? Math.max(1, Math.ceil(historyTotal / pageSize))
    : 1
  const canPrev = page > 1
  const canNext = page < totalPages

  const statusType = useMemo(() => {
    const s = (task?.state || '').toLowerCase()
    if (!s) return 'default'
    if (s.includes('wait')) return 'waiting'
    if (s.includes('run') || s.includes('process')) return 'running'
    if (s === 'success' || s === 'succeeded' || s === 'ok') return 'success'
    if (s.includes('timeout')) return 'timeout'
    if (s.includes('fail') || s.includes('error')) return 'fail'
    return 'default'
  }, [task])

  // -------- 上传文件相关 --------
  function applyFile(f) {
    if (!f) {
      setFile(null)
      setFilePreview(null)
      return
    }
    setFile(f)
    try {
      const url = URL.createObjectURL(f)
      setFilePreview(url)
    } catch {
      setFilePreview(null)
    }
  }

  function onFileInputChange(e) {
    const f = e.target.files?.[0]
    applyFile(f || null)
  }

  function onDrop(e) {
    e.preventDefault()
    setDragOver(false)
    const f = e.dataTransfer.files?.[0]
    if (f) applyFile(f)
  }

  // -------- Storyboard 分镜操作 --------
  function addShot() {
    setShots((list) => [...list, { duration: 5, scene: '' }])
  }

  function updateShot(index, patch) {
    setShots((list) =>
      list.map((s, i) => (i === index ? { ...s, ...patch } : s)),
    )
  }

  function removeShot(index) {
    setShots((list) => list.filter((_, i) => i !== index))
  }

  // -------- 创建任务 --------
  async function onSubmit(e) {
    e.preventDefault()
    if (!wid) {
      // eslint-disable-next-line no-alert
      alert('缺少 workspace id')
      return
    }

    const kind = currentModel.kind

    // 批量去水印：多行链接，一个链接一个任务，同时最多 10 个请求在跑
    if (kind === 'watermark') {
      const lines = (videoUrl || '')
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean)

      if (!lines.length) {
        // eslint-disable-next-line no-alert
        alert('请至少输入一个 Sora 视频链接')
        return
      }

      setSubmitting(true)
      setErr('')

      let created = 0
      let failed = 0
      let lastCreatedTaskId = null

      try {
        await runWithConcurrency(lines, 10, async (urlStr, idx) => {
          try {
            const resp = await kieTenantApi.createSora2Task(wid, {
              modelId,
              video_url: urlStr,
            })
            const newTask = resp?.task || resp
            if (newTask?.id) {
              lastCreatedTaskId = newTask.id
              if (typeof window !== 'undefined') {
                window.localStorage.setItem(lastTaskKey, String(newTask.id))
              }
            }
            created += 1
          } catch (err2) {
            // eslint-disable-next-line no-console
            console.error('创建去水印任务失败', idx, err2)
            failed += 1
          }
        })

        if (lastCreatedTaskId != null) {
          setCurrentTaskId(lastCreatedTaskId)
        }

        setPage(1)
        await refetchHistory()
        if (lastCreatedTaskId != null) {
          await refetchTask()
        }

        // eslint-disable-next-line no-alert
        alert(`去水印任务已创建：成功 ${created} 条，失败 ${failed} 条`)
      } catch (e2) {
        // eslint-disable-next-line no-console
        console.error(e2)
        setErr(e2?.message || '创建任务失败')
      } finally {
        setSubmitting(false)
      }

      return
    }

    // 其他模型：单任务
    if (currentModel.hasPrompt) {
      if (!prompt.trim()) {
        // eslint-disable-next-line no-alert
        alert('请输入提示词')
        return
      }
    }

    if (currentModel.hasImageUpload && kind !== 'storyboard' && !file) {
      // eslint-disable-next-line no-alert
      alert('请先选择一张图片')
      return
    }

    if (kind === 'storyboard') {
      const validShots = shots
        .filter((s) => s && s.scene && s.scene.trim())
        .map((s) => ({
          Scene: s.scene.trim(),
          duration: Number(s.duration) || 1,
        }))
      if (!validShots.length) {
        // eslint-disable-next-line no-alert
        alert('请至少填写一个分镜场景')
        return
      }
    } else if (!nFrames || Number.isNaN(Number(nFrames))) {
      // eslint-disable-next-line no-alert
      alert('请选择视频时长')
      return
    }

    setSubmitting(true)
    setErr('')
    try {
      const payload = {
        modelId,
        prompt: prompt.slice(0, MAX_PROMPT_LEN),
        aspect_ratio: kind === 'watermark' ? undefined : aspectRatio,
        n_frames: kind === 'watermark' ? undefined : nFrames,
        remove_watermark: currentModel.hasWatermarkToggle
          ? removeWatermark
          : undefined,
        size: currentModel.hasSize ? size : undefined,
        image: currentModel.hasImageUpload ? file : undefined,
        video_url: undefined,
        shots: undefined,
      }

      if (kind === 'storyboard') {
        payload.shots = shots
          .filter((s) => s && s.scene && s.scene.trim())
          .map((s) => ({
            Scene: s.scene.trim(),
            duration: Number(s.duration) || 1,
          }))
      }

      const resp = await kieTenantApi.createSora2Task(wid, payload)

      const newTask = resp?.task || resp
      const newTaskId = newTask?.id
      if (!newTaskId) {
        throw new Error('创建成功但未返回任务 ID')
      }

      setCurrentTaskId(newTaskId)
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(lastTaskKey, String(newTaskId))
      }

      setPage(1)
      await refetchTask()
      await refetchHistory()

      // eslint-disable-next-line no-alert
      alert('任务已创建')
    } catch (e2) {
      // eslint-disable-next-line no-console
      console.error(e2)
      setErr(e2?.message || '创建任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  // 手动刷新当前任务
  async function onRefreshTask() {
    if (!currentTaskId) return
    await refetchTask()
    await refetchHistory()
  }

  // 清除当前任务（不动历史）
  function onClearTask() {
    setCurrentTaskId(null)
    setPreview(null)
    if (typeof window !== 'undefined') {
      window.localStorage.removeItem(lastTaskKey)
    }
    queryClient.removeQueries({ queryKey: ['sora2-task', wid, modelId] })
    queryClient.removeQueries({ queryKey: ['sora2-files', wid, modelId] })
  }

  // 清空任务历史（会删除数据库记录）
  async function onClearHistory() {
    if (!wid || !historyTotal) return
    // eslint-disable-next-line no-alert
    const ok = window.confirm(
      '确定要清空当前模型的任务记录吗？\n此操作会删除数据库中的任务及关联文件记录，且不可恢复。',
    )
    if (!ok) return

    try {
      await kieTenantApi.clearTasks(wid, { modelId })

      // 清理前端状态
      setCurrentTaskId(null)
      setPreview(null)
      if (typeof window !== 'undefined') {
        window.localStorage.removeItem(lastTaskKey)
      }
      queryClient.removeQueries({ queryKey: ['sora2-task', wid, modelId] })
      queryClient.removeQueries({ queryKey: ['sora2-files', wid, modelId] })

      setPage(1)
      await refetchHistory()
      // eslint-disable-next-line no-alert
      alert('任务记录已清空')
    } catch (e2) {
      // eslint-disable-next-line no-console
      console.error(e2)
      // eslint-disable-next-line no-alert
      alert(e2?.message || '清空任务记录失败')
    }
  }

  // 下载视频
  async function handleDownload(fileId) {
    if (!wid || !fileId) return
    try {
      const url = await kieTenantApi.getFileDownloadUrl(wid, fileId)
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
      // eslint-disable-next-line no-alert
      alert(e2?.message || '获取下载链接失败')
    }
  }

  // 预览视频
  async function handlePreview(fileObj) {
    if (!wid || !fileObj?.id) return
    try {
      const url = await kieTenantApi.getFileDownloadUrl(wid, fileObj.id)
      if (!url) return
      setPreview({
        url,
        kind: fileObj.kind,
        mime: fileObj.mime_type || '',
      })
    } catch (e2) {
      // eslint-disable-next-line no-console
      console.error(e2)
      // eslint-disable-next-line no-alert
      alert(e2?.message || '获取预览链接失败')
    }
  }

  function closePreview() {
    setPreview(null)
  }

  const promptCharsLeft = MAX_PROMPT_LEN - (prompt?.length || 0)

  const durationOptions =
    currentModel.kind === 'storyboard'
      ? STORYBOARD_DURATION_OPTIONS
      : DURATION_OPTIONS

  const pageTitle = (() => {
    switch (modelId) {
      case 'sora-2-text-to-video':
        return 'Sora 2 文本生成视频'
      case 'sora-2-pro-text-to-video':
        return 'Sora 2 Pro 文本生成视频'
      case 'sora-2-image-to-video':
        return 'Sora 2 图片生成视频'
      case 'sora-2-pro-image-to-video':
        return 'Sora 2 Pro 图片生成视频'
      case 'sora-2-pro-storyboard':
        return 'Sora 2 Pro 分镜生成视频'
      case 'sora-watermark-remover':
        return 'Sora 2 视频去水印'
      default:
        return 'Sora 2 视频'
    }
  })()

  const pageIntro = (() => {
    switch (currentModel.kind) {
      case 'text':
      case 'text-pro':
        return '输入一段文案提示词，调用 Sora 2 文本转视频模型生成 10 / 15 秒竖屏或横屏视频。'
      case 'image':
      case 'image-pro':
        return '上传一张产品图片 + 文案提示词，调用 Sora 2 图片转视频模型生成 10 / 15 秒视频。'
      case 'storyboard':
        return '上传一张参考图（可选），并填写分镜故事线，调用 Sora 2 Pro Storyboard 自动生成多镜头视频。'
      case 'watermark':
        return '粘贴一个或多个 Sora 官方链接（每行一个），调用 Sora Watermark Remover 批量生成去水印版本。'
      default:
        return ''
    }
  })()

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
            onClick={() => setModelId(m.id)}
            className={modelId === m.id ? 'btn sm' : 'btn ghost sm'}
            style={{ fontSize: 13 }}
          >
            {m.label}
          </button>
        ))}
      </div>

      <p className="small-muted" style={{ marginBottom: 16 }}>
        {pageIntro}
      </p>

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
          {currentModel.hasPrompt && (
            <div style={{ gridColumn: '1 / -1' }}>
              <FormField label={`提示词（英文建议，最多 ${MAX_PROMPT_LEN} 字符）`}>
                <div style={{ position: 'relative' }}>
                  <textarea
                    rows={8}
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                    maxLength={MAX_PROMPT_LEN}
                    placeholder="请详细描述你希望生成的视频内容、风格、镜头、时长等..."
                    style={{
                      width: '100%',
                      resize: 'vertical',
                      fontFamily: 'inherit',
                      borderRadius: 8,
                      paddingRight: 90,
                    }}
                  />
                  <span
                    className="small-muted"
                    style={{
                      position: 'absolute',
                      right: 8,
                      bottom: 6,
                      fontSize: 11,
                      opacity: 0.7,
                    }}
                  >
                    {promptCharsLeft} 剩余
                  </span>
                </div>
              </FormField>
            </div>
          )}

          {/* 水印去除模型的 URL 输入（多行） */}
          {currentModel.kind === 'watermark' && (
            <div style={{ gridColumn: '1 / -1' }}>
              <FormField label="Sora 视频链接（每行一个 https://sora.chatgpt.com/...）">
                <textarea
                  rows={6}
                  value={videoUrl}
                  onChange={(e) => setVideoUrl(e.target.value)}
                  placeholder={
                    '示例：\nhttps://sora.chatgpt.com/share/xxx\nhttps://sora.chatgpt.com/share/yyy'
                  }
                  style={{ width: '100%', resize: 'vertical' }}
                />
                <p className="small-muted" style={{ marginTop: 4 }}>
                  同时最多并发创建 10 个任务，多余的会在浏览器中排队依次创建。
                </p>
              </FormField>
            </div>
          )}

          {/* 画面比例 */}
          {currentModel.kind !== 'watermark' && (
            <FormField label="画面比例">
              <div style={{ display: 'inline-flex', gap: 8 }}>
                {ASPECT_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setAspectRatio(opt.value)}
                    className={aspectRatio === opt.value ? 'btn' : 'btn ghost'}
                    style={{ padding: '6px 14px', fontSize: 13 }}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </FormField>
          )}

          {/* 视频时长 */}
          {currentModel.kind !== 'watermark' && (
            <FormField label="视频时长">
              <div style={{ display: 'inline-flex', gap: 8 }}>
                {durationOptions.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setNFrames(opt.value)}
                    className={nFrames === opt.value ? 'btn' : 'btn ghost'}
                    style={{ padding: '6px 14px', fontSize: 13 }}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </FormField>
          )}

          {/* Pro 模型 size 选择 */}
          {currentModel.hasSize && (
            <FormField label="画质 / 规格">
              <div style={{ display: 'inline-flex', gap: 8 }}>
                <button
                  type="button"
                  className={size === 'standard' ? 'btn' : 'btn ghost'}
                  style={{ padding: '4px 12px', fontSize: 13 }}
                  onClick={() => setSize('standard')}
                >
                  Standard
                </button>
                <button
                  type="button"
                  className={size === 'high' ? 'btn' : 'btn ghost'}
                  style={{ padding: '4px 12px', fontSize: 13 }}
                  onClick={() => setSize('high')}
                >
                  High
                </button>
              </div>
            </FormField>
          )}

          {/* 水印开关（仅部分模型） */}
          {currentModel.hasWatermarkToggle && (
            <FormField label="水印">
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                  <button
                    type="button"
                    className={!removeWatermark ? 'btn' : 'btn ghost'}
                    style={{ padding: '4px 10px', fontSize: 13 }}
                    onClick={() => setRemoveWatermark(false)}
                  >
                    保留水印
                  </button>
                  <button
                    type="button"
                    className={removeWatermark ? 'btn' : 'btn ghost'}
                    style={{ padding: '4px 10px', fontSize: 13 }}
                    onClick={() => setRemoveWatermark(true)}
                  >
                    去除水印
                  </button>
                </div>
              </div>
            </FormField>
          )}

          {/* Storyboard 分镜编辑器 */}
          {currentModel.kind === 'storyboard' && (
            <div style={{ gridColumn: '1 / -1' }}>
              <FormField label="分镜故事线（必填，每一行代表一个镜头）">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {shots.map((shot, idx) => (
                    <div
                      key={idx}
                      style={{
                        display: 'grid',
                        gridTemplateColumns: '90px minmax(0, 1fr) 70px',
                        gap: 8,
                        alignItems: 'center',
                      }}
                    >
                      <input
                        type="number"
                        min="1"
                        step="0.5"
                        value={shot.duration}
                        onChange={(e) =>
                          updateShot(idx, {
                            duration: Number(e.target.value) || 1,
                          })
                        }
                        style={{
                          width: '100%',
                          padding: '4px 6px',
                          borderRadius: 6,
                          border: '1px solid var(--border)',
                          fontSize: 13,
                        }}
                        placeholder="时长（秒）"
                      />
                      <input
                        type="text"
                        value={shot.scene}
                        onChange={(e) => updateShot(idx, { scene: e.target.value })}
                        style={{
                          width: '100%',
                          padding: '4px 6px',
                          borderRadius: 6,
                          border: '1px solid var(--border)',
                          fontSize: 13,
                        }}
                        placeholder="场景描述 / 提示词"
                      />
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => removeShot(idx)}
                        disabled={shots.length <= 1}
                      >
                        删除
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className="btn ghost sm"
                    onClick={addShot}
                    style={{ alignSelf: 'flex-start', marginTop: 4 }}
                  >
                    + 添加分镜
                  </button>
                </div>
              </FormField>
            </div>
          )}

          {/* 图片上传 */}
          {currentModel.hasImageUpload && (
            <FormField label="源图片（PNG/JPG，≤ 20MB）">
              <div
                onDragOver={(e) => {
                  e.preventDefault()
                  setDragOver(true)
                }}
                onDragLeave={(e) => {
                  e.preventDefault()
                  setDragOver(false)
                }}
                onDrop={onDrop}
                style={{
                  border: dragOver ? '2px dashed #2563eb' : '2px dashed #e5e7eb',
                  borderRadius: 10,
                  padding: 16,
                  textAlign: 'center',
                  cursor: 'pointer',
                  backgroundColor: dragOver ? '#eff6ff' : '#fafafa',
                }}
              >
                <p style={{ margin: 0, fontSize: 13 }}>
                  拖拽图片到此处，或{' '}
                  <label style={{ color: '#2563eb', cursor: 'pointer' }}>
                    点击选择
                    <input
                      type="file"
                      accept="image/*"
                      style={{ display: 'none' }}
                      onChange={onFileInputChange}
                    />
                  </label>
                </p>
                {file && (
                  <div style={{ marginTop: 8 }} className="small-muted">
                    已选择：{file.name}（{Math.round(file.size / 1024)} KB）
                  </div>
                )}
                {filePreview && (
                  <div style={{ marginTop: 12 }}>
                    <img
                      src={filePreview}
                      alt="预览"
                      style={{
                        maxWidth: '100%',
                        maxHeight: 220,
                        borderRadius: 8,
                        boxShadow: '0 2px 6px rgba(0,0,0,0.08)',
                      }}
                    />
                  </div>
                )}
              </div>
            </FormField>
          )}

          <div style={{ marginTop: 8 }}>
            <button className="btn" type="submit" disabled={submitting}>
              {submitting ? '创建中…' : '创建任务'}
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
                  <div
                    style={{
                      marginTop: 4,
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                    }}
                  >
                    <strong>状态：</strong>
                    <Badge type={statusType}>
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
                          statusType === 'waiting'
                            ? '20%'
                            : statusType === 'running'
                              ? '60%'
                              : '100%',
                        background:
                          statusType === 'success'
                            ? '#16a34a'
                            : statusType === 'fail' || statusType === 'timeout'
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
                            </div>
                          </li>
                        ))}
                    </ul>
                  ) : (
                    <div className="small-muted">暂无文件，请稍后刷新状态。</div>
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
              <h3 style={{ margin: 0 }}>任务记录</h3>
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
                  style={{ maxHeight: 260, overflow: 'auto' }}
                >
                  <table className="table">
                    <thead>
                      <tr>
                        <th style={{ width: 80 }}>ID</th>
                        <th style={{ width: 90 }}>状态</th>
                        <th>提示摘要 / 链接</th>
                        <th style={{ width: 120 }}>创建时间</th>
                        <th style={{ width: 80 }}>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {history.map((t) => (
                        <tr key={t.id}>
                          <td>{t.id}</td>
                          <td>
                            <Badge
                              type={(() => {
                                const s = (t.state || '').toLowerCase()
                                if (!s) return 'default'
                                if (s.includes('wait')) return 'waiting'
                                if (
                                  s.includes('run') ||
                                  s.includes('process')
                                ) {
                                  return 'running'
                                }
                                if (
                                  s === 'success' ||
                                  s === 'succeeded' ||
                                  s === 'ok'
                                ) {
                                  return 'success'
                                }
                                if (s.includes('timeout')) return 'timeout'
                                if (
                                  s.includes('fail') ||
                                  s.includes('error')
                                ) {
                                  return 'fail'
                                }
                                return 'default'
                              })()}
                            >
                              {t.state}
                            </Badge>
                          </td>
                          <td className="small-muted">
                            {(t.prompt || '').slice(0, 40)}
                            {t.prompt && t.prompt.length > 40 ? '…' : ''}
                          </td>
                          <td className="small-muted">
                            {t.created_at
                              ? new Date(t.created_at).toLocaleString()
                              : ''}
                          </td>
                          <td>
                            <button
                              type="button"
                              className="btn ghost"
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
    </div>
  )
}

