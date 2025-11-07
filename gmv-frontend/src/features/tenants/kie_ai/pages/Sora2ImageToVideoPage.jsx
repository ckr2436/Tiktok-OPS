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

function getLastTaskKey (wid) {
  return `${LAST_TASK_KEY_PREFIX}${wid ?? ''}`
}

const ASPECT_OPTIONS = [
  { value: 'portrait', label: '竖屏 9:16' },
  { value: 'landscape', label: '横屏 16:9' },
]

const DURATION_OPTIONS = [
  { value: 10, label: '10 秒' },
  { value: 15, label: '15 秒' },
]

const PAGE_SIZE_OPTIONS = [10, 20, 50]

function Badge ({ type = 'default', children }) {
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

function shouldPollByState (state) {
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

export default function Sora2ImageToVideoPage () {
  const { wid } = useParams()
  const queryClient = useQueryClient()

  const [prompt, setPrompt] = useState('')
  const [aspectRatio, setAspectRatio] = useState('portrait')
  const [nFrames, setNFrames] = useState(10)
  const [removeWatermark, setRemoveWatermark] = useState(true)

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

  const lastTaskKey = getLastTaskKey(wid)

  // 挂载时恢复最近任务 ID
  useEffect(() => {
    if (!wid || typeof window === 'undefined') return
    const lastId = window.localStorage.getItem(lastTaskKey)
    if (lastId) {
      setCurrentTaskId(Number(lastId) || lastId)
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
    queryKey: ['sora2-task', wid, currentTaskId],
    queryFn: () => kieTenantApi.getTask(wid, currentTaskId, { refresh: true }),
    enabled: !!wid && !!currentTaskId,
    refetchInterval: query =>
      shouldPollByState(query.state.data?.state) ? 8000 : false,
  })

  // ------- React Query：当前任务文件 -------
  const {
    data: files = [],
  } = useQuery({
    queryKey: ['sora2-files', wid, currentTaskId],
    queryFn: () => kieTenantApi.listTaskFiles(wid, currentTaskId),
    enabled: !!wid && !!currentTaskId,
    refetchInterval: () => {
      if (!task) return 8000
      return shouldPollByState(task.state) ? 8000 : false
    },
  })

  // ------- React Query：任务历史（仅在需要时刷）-------
  const {
    data: historyResp,
    isLoading: historyLoading,
    refetch: refetchHistory,
  } = useQuery({
    queryKey: ['sora2-history', wid, page, pageSize],
    queryFn: () => kieTenantApi.listTasks(wid, { page, size: pageSize }),
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
  function applyFile (f) {
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

  function onFileInputChange (e) {
    const f = e.target.files?.[0]
    applyFile(f || null)
  }

  function onDrop (e) {
    e.preventDefault()
    setDragOver(false)
    const f = e.dataTransfer.files?.[0]
    if (f) applyFile(f)
  }

  // -------- 创建任务 --------
  async function onSubmit (e) {
    e.preventDefault()
    if (!wid) {
      alert('缺少 workspace id')
      return
    }
    if (!file) {
      alert('请先选择一张图片')
      return
    }
    if (!prompt.trim()) {
      alert('请输入提示词')
      return
    }

    setSubmitting(true)
    setErr('')
    try {
      const resp = await kieTenantApi.createImageToVideoTask(wid, {
        prompt: prompt.slice(0, MAX_PROMPT_LEN),
        aspect_ratio: aspectRatio,
        n_frames: nFrames,
        remove_watermark: removeWatermark,
        image: file,
      })

      const newTask = resp?.task || resp
      const newTaskId = newTask?.id
      if (!newTaskId) {
        throw new Error('创建成功但未返回任务 ID')
      }

      setCurrentTaskId(newTaskId)
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(lastTaskKey, String(newTaskId))
      }

      // 回到第一页，刷新当前任务 + 历史
      setPage(1)
      await refetchTask()
      await refetchHistory()

      alert('任务已创建')
    } catch (e2) {
      console.error(e2)
      setErr(e2?.message || '创建任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  // 手动刷新当前任务
  async function onRefreshTask () {
    if (!currentTaskId) return
    await refetchTask()
    await refetchHistory()
  }

  // 清除当前任务
  function onClearTask () {
    setCurrentTaskId(null)
    setPreview(null)
    if (typeof window !== 'undefined') {
      window.localStorage.removeItem(lastTaskKey)
    }
    queryClient.removeQueries({ queryKey: ['sora2-task', wid] })
    queryClient.removeQueries({ queryKey: ['sora2-files', wid] })
  }

  // 下载视频
  async function handleDownload (fileId) {
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
      console.error(e2)
      alert(e2?.message || '获取下载链接失败')
    }
  }

  // 预览视频
  async function handlePreview (fileObj) {
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
      console.error(e2)
      alert(e2?.message || '获取预览链接失败')
    }
  }

  function closePreview () {
    setPreview(null)
  }

  const promptCharsLeft = MAX_PROMPT_LEN - (prompt?.length || 0)

  return (
    <div className="card card--elevated">
      <h2 style={{ marginTop: 0 }}>Sora 2 - 图片生成视频</h2>

      <p className="small-muted" style={{ marginBottom: 16 }}>
        上传一张产品图片 + 文案提示词，调用 Sora 2 模型生成 10s / 15s 竖屏或横屏视频。
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
          {/* 提示词独立一行 */}
          <div style={{ gridColumn: '1 / -1' }}>
            <FormField label={`提示词（英文建议，最多 ${MAX_PROMPT_LEN} 字符）`}>
              <div style={{ position: 'relative' }}>
                <textarea
                  rows={8}
                  value={prompt}
                  onChange={e => setPrompt(e.target.value)}
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

          <FormField label="画面比例">
            <div style={{ display: 'inline-flex', gap: 8 }}>
              {ASPECT_OPTIONS.map(opt => (
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

          <FormField label="视频时长">
            <div style={{ display: 'inline-flex', gap: 8 }}>
              {DURATION_OPTIONS.map(opt => (
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

          <FormField label="源图片（PNG/JPG，≤ 20MB）">
            <div
              onDragOver={e => {
                e.preventDefault()
                setDragOver(true)
              }}
              onDragLeave={e => {
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

          <div style={{ marginTop: 8 }}>
            <button
              className="btn"
              type="submit"
              disabled={submitting}
            >
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
                    <button className="btn ghost" type="button" onClick={onRefreshTask}>
                      刷新状态
                    </button>
                    <button className="btn ghost" type="button" onClick={onClearTask}>
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
                  <div style={{ marginTop: 4, display: 'flex', alignItems: 'center', gap: 8 }}>
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
                    <summary className="small-muted">查看提示词</summary>
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
                        .filter(f => f.kind === 'result')
                        .map(f => (
                          <li key={f.id} style={{ marginBottom: 6 }}>
                            <div>[结果文件]</div>
                            <div style={{ marginTop: 4, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
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
              <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                <label className="small-muted" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  每页
                  <select
                    value={pageSize}
                    onChange={e => setPageSize(Number(e.target.value) || 10)}
                    style={{
                      padding: '4px 8px',
                      borderRadius: 8,
                      border: '1px solid var(--border)',
                      background: 'var(--panel-2)',
                      color: 'inherit',
                      fontSize: 12,
                    }}
                  >
                    {PAGE_SIZE_OPTIONS.map(sz => (
                      <option key={sz} value={sz}>{sz}</option>
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
              </div>
            </div>

            {historyLoading && <Loading />}

            {!historyLoading && history.length === 0 && (
              <div className="small-muted">暂无历史记录。</div>
            )}

            {!historyLoading && history.length > 0 && (
              <>
                <div className="table-wrapper" style={{ maxHeight: 260, overflow: 'auto' }}>
                  <table className="table">
                    <thead>
                      <tr>
                        <th style={{ width: 80 }}>ID</th>
                        <th style={{ width: 90 }}>状态</th>
                        <th>提示词摘要</th>
                        <th style={{ width: 120 }}>创建时间</th>
                        <th style={{ width: 80 }}>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {history.map(t => (
                        <tr key={t.id}>
                          <td>{t.id}</td>
                          <td>
                            <Badge
                              type={(() => {
                                const s = (t.state || '').toLowerCase()
                                if (!s) return 'default'
                                if (s.includes('wait')) return 'waiting'
                                if (s.includes('run') || s.includes('process')) return 'running'
                                if (s === 'success' || s === 'succeeded' || s === 'ok') return 'success'
                                if (s.includes('timeout')) return 'timeout'
                                if (s.includes('fail') || s.includes('error')) return 'fail'
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
                            {t.created_at ? new Date(t.created_at).toLocaleString() : ''}
                          </td>
                          <td>
                            <button
                              type="button"
                              className="btn ghost"
                              onClick={() => {
                                setCurrentTaskId(t.id)
                                if (typeof window !== 'undefined') {
                                  window.localStorage.setItem(lastTaskKey, String(t.id))
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
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <button
                      type="button"
                      className="btn ghost sm"
                      disabled={!canPrev}
                      onClick={() => canPrev && setPage(p => p - 1)}
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
                      onClick={() => canNext && setPage(p => p + 1)}
                    >
                      下一页
                    </button>
                  </div>
                </div>
              </>
            )}
          </section>

          <p className="small-muted" style={{ marginTop: 16 }}>
            提示：视频实际画质、时长、转场效果由模型与平台控制，当前不做内容审核。建议在提示词中描述完整的
            10s / 15s 剧本。
          </p>
        </div>
      </div>

      {/* 预览弹窗：视频小卡片 */}
      {preview && (
        <div className="modal-backdrop" onClick={closePreview}>
          <div
            className="modal"
            onClick={e => e.stopPropagation()}
            style={{ width: 'min(820px, 92vw)' }}
          >
            <div className="modal__header">
              <div className="modal__title">视频预览</div>
              <button className="modal__close" type="button" onClick={closePreview}>
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

