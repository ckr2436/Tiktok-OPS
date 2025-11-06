// src/features/tenants/kie_ai/pages/Sora2ImageToVideoPage.jsx
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import kieTenantApi from '../service.js'
import FormField from '../../../../components/ui/FormField.jsx'
import Loading from '../../../../components/ui/Loading.jsx'
import CopyButton from '../../../../components/CopyButton.jsx'

const ASPECT_OPTIONS = [
  { value: 'portrait', label: '竖屏 (9:16)' },
  { value: 'landscape', label: '横屏 (16:9)' },
]

const N_FRAME_OPTIONS = [
  { value: 10, label: '10 秒' },
  { value: 15, label: '15 秒' },
]

const POLL_INTERVAL = 3500

export default function Sora2ImageToVideoPage() {
  const { workspaceId } = useParams()
  const wsId = workspaceId || ''

  const [prompt, setPrompt] = useState('')
  const [aspectRatio, setAspectRatio] = useState('portrait')
  const [nFrames, setNFrames] = useState(10)
  const [removeWatermark, setRemoveWatermark] = useState(true)
  const [imageFile, setImageFile] = useState(null)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')

  const [task, setTask] = useState(null)
  const [files, setFiles] = useState([])
  const [polling, setPolling] = useState(false)
  const [pollError, setPollError] = useState('')
  const [downloadMap, setDownloadMap] = useState({}) // {fileId: url}

  const resetResult = () => {
    setTask(null)
    setFiles([])
    setDownloadMap({})
    setPollError('')
  }

  const handleFileChange = (e) => {
    const f = e.target.files?.[0]
    if (!f) {
      setImageFile(null)
      return
    }
    if (!f.type.startsWith('image/')) {
      window.alert('仅支持图片文件')
      e.target.value = ''
      return
    }
    if (f.size > 20 * 1024 * 1024) {
      window.alert('图片不能超过 20MB')
      e.target.value = ''
      return
    }
    setImageFile(f)
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (creating) return
    setCreateError('')

    if (!prompt.trim()) {
      setCreateError('提示词不能为空')
      return
    }
    if (!imageFile) {
      setCreateError('请选择一张图片')
      return
    }

    setCreating(true)
    resetResult()
    try {
      const resp = await kieTenantApi.createSora2Task(wsId, {
        prompt: prompt.trim(),
        aspect_ratio: aspectRatio,
        n_frames: nFrames,
        remove_watermark: removeWatermark,
        imageFile,
      })
      setTask(resp.task)
      // 上传文件也在 resp.upload_file 里，但我们统一从后端查询
      setFiles(resp.upload_file ? [resp.upload_file] : [])
      setPollError('')
      setPolling(true)
    } catch (err) {
      setCreateError(err?.message || '创建任务失败')
    } finally {
      setCreating(false)
    }
  }

  const refreshTaskAndFiles = useCallback(async () => {
    if (!task?.id) return
    try {
      const t = await kieTenantApi.getSora2Task(wsId, task.id, { refresh: true })
      setTask(t)
      const fs = await kieTenantApi.listTaskFiles(wsId, task.id)
      setFiles(fs)
    } catch (err) {
      setPollError(err?.message || '刷新任务状态失败')
    }
  }, [task?.id, wsId])

  // 轮询：任务非终态时每隔几秒刷新一次
  useEffect(() => {
    if (!polling || !task?.state) return

    if (['success', 'fail', 'failed', 'error'].includes(task.state)) {
      setPolling(false)
      return
    }

    const timer = setTimeout(() => {
      refreshTaskAndFiles()
    }, POLL_INTERVAL)

    return () => clearTimeout(timer)
  }, [polling, task?.state, refreshTaskAndFiles])

  const handleManualRefresh = async () => {
    await refreshTaskAndFiles()
    if (task?.state && !['success', 'fail', 'failed', 'error'].includes(task.state)) {
      setPolling(true)
    }
  }

  const handleGetDownloadUrl = async (file) => {
    try {
      const url = await kieTenantApi.getFileDownloadUrl(wsId, file.id)
      setDownloadMap((m) => ({ ...m, [file.id]: url }))
    } catch (err) {
      window.alert(err?.message || '获取下载链接失败')
    }
  }

  const statusLabel = useMemo(() => {
    if (!task?.state) return ''
    const s = String(task.state).toLowerCase()
    if (s === 'waiting' || s === 'queuing') return '排队中'
    if (s === 'generating') return '生成中'
    if (s === 'success') return '完成'
    if (s === 'fail' || s === 'failed' || s === 'error') return '失败'
    return task.state
  }, [task?.state])

  return (
    <div>
      <header className="page-header">
        <h1 className="page-title">KIE Sora 2 - 图片生成视频</h1>
        <p className="page-subtitle">
          上传一张产品图 + 文案提示词，调用 KIE AI 的 Sora 2 模型生成营销视频。
        </p>
      </header>

      <div className="grid grid--2col">
        <section className="card">
          <div className="card__header">
            <h2 className="card__title">创建任务</h2>
          </div>
          <div className="card__body">
            <form className="form vertical" onSubmit={handleSubmit}>
              <FormField label="提示词（英文建议，最多 10000 字符）">
                <textarea
                  className="input input--textarea"
                  rows={10}
                  maxLength={10_000}
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  placeholder="例如：10-second vertical video for TikTok..."
                />
              </FormField>

              <FormField label="画幅比例">
                <select
                  className="input"
                  value={aspectRatio}
                  onChange={(e) => setAspectRatio(e.target.value)}
                >
                  {ASPECT_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </FormField>

              <FormField label="视频时长">
                <select
                  className="input"
                  value={nFrames}
                  onChange={(e) => setNFrames(Number(e.target.value))}
                >
                  {N_FRAME_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </FormField>

              <FormField label="水印">
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={removeWatermark}
                    onChange={(e) => setRemoveWatermark(e.target.checked)}
                  />
                  <span>去除水印（若模型支持）</span>
                </label>
              </FormField>

              <FormField label="源图片（PNG/JPG，≤ 20MB）">
                <input type="file" accept="image/*" onChange={handleFileChange} />
              </FormField>

              {createError && (
                <div className="form__error" style={{ marginBottom: 8 }}>
                  {createError}
                </div>
              )}

              <div className="form__actions">
                <button
                  type="submit"
                  className="btn btn--primary"
                  disabled={creating}
                >
                  {creating ? '创建中...' : '创建任务'}
                </button>
              </div>

              <p className="muted" style={{ marginTop: 12, fontSize: 12 }}>
                建议：提示词尽量具体，包含镜头、情绪、时长等信息。当前 demo 不做分段拼接，由你在提示词中描述完整 10s/15s 的脚本。
              </p>
            </form>
          </div>
        </section>

        <section className="card">
          <div className="card__header">
            <h2 className="card__title">任务状态 & 结果</h2>
            {task && (
              <div className="card__extra">
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={handleManualRefresh}
                >
                  手动刷新
                </button>
              </div>
            )}
          </div>
          <div className="card__body">
            {!task ? (
              <div className="empty">还没有任务，请先在左侧创建。</div>
            ) : (
              <>
                <div className="kv">
                  <div className="kv__row">
                    <div className="kv__key">任务 ID</div>
                    <div className="kv__value">{task.id}</div>
                  </div>
                  <div className="kv__row">
                    <div className="kv__key">外部 TaskId</div>
                    <div className="kv__value small-muted">{task.task_id}</div>
                  </div>
                  <div className="kv__row">
                    <div className="kv__key">模型</div>
                    <div className="kv__value">{task.model}</div>
                  </div>
                  <div className="kv__row">
                    <div className="kv__key">状态</div>
                    <div className="kv__value">
                      {statusLabel}
                      {polling && <span className="small-muted">（轮询中...）</span>}
                    </div>
                  </div>
                </div>

                {pollError && (
                  <div className="alert alert--error" style={{ marginTop: 8 }}>
                    {pollError}
                  </div>
                )}

                <hr style={{ margin: '16px 0' }} />

                <h3 className="section-title">生成结果</h3>
                {files.length === 0 ? (
                  <div className="empty small">暂未查询到文件记录，请稍后刷新。</div>
                ) : (
                  <div className="table-wrapper">
                    <table className="table table--compact">
                      <thead>
                        <tr>
                          <th>ID</th>
                          <th>类型</th>
                          <th>文件 URL</th>
                          <th>下载链接（20 分钟）</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {files.map((f) => {
                          const dl = downloadMap[f.id]
                          return (
                            <tr key={f.id}>
                              <td>{f.id}</td>
                              <td>{f.kind}</td>
                              <td>
                                <a
                                  href={f.file_url}
                                  target="_blank"
                                  rel="noopener"
                                  className="small-muted"
                                >
                                  源地址
                                </a>
                              </td>
                              <td>
                                {dl ? (
                                  <div className="download-url-cell">
                                    <a
                                      href={dl}
                                      target="_blank"
                                      rel="noopener"
                                      className="small-muted"
                                    >
                                      打开
                                    </a>
                                    <CopyButton
                                      text={dl}
                                      size="sm"
                                      className="ml-2"
                                    />
                                  </div>
                                ) : (
                                  <span className="small-muted">尚未获取</span>
                                )}
                              </td>
                              <td>
                                <button
                                  type="button"
                                  className="btn btn--sm"
                                  onClick={() => handleGetDownloadUrl(f)}
                                >
                                  获取下载 URL
                                </button>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}

                {task?.prompt && (
                  <>
                    <hr style={{ margin: '16px 0' }} />
                    <details>
                      <summary>查看本次提示词</summary>
                      <pre className="prompt-preview">
                        {task.prompt}
                      </pre>
                    </details>
                  </>
                )}
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}

