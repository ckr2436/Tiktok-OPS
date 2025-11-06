// src/features/tenants/kie_ai/pages/Sora2ImageToVideoPage.jsx
import { useCallback, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import kieTenantApi from '../service.js'
import FormField from '../../../../components/ui/FormField.jsx'
import Loading from '../../../../components/ui/Loading.jsx'

const PROMPT_MAX = 10_000

const aspectOptions = [
  { value: 'portrait', label: '竖屏 (9:16)' },
  { value: 'landscape', label: '横屏 (16:9)' },
]

const frameOptions = [
  { value: '10', label: '10 秒' },
  { value: '15', label: '15 秒' },
]

export default function Sora2ImageToVideoPage() {
  const { wid } = useParams()    // ★ 直接从路由拿 workspace_id

  const [prompt, setPrompt] = useState('')
  const [aspectRatio, setAspectRatio] = useState('portrait')
  const [nFrames, setNFrames] = useState('10')
  const [removeWatermark, setRemoveWatermark] = useState(true)
  const [imageFile, setImageFile] = useState(null)

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [task, setTask] = useState(null)
  const [files, setFiles] = useState([])
  const [loadingTask, setLoadingTask] = useState(false)

  const promptCount = useMemo(() => prompt.length, [prompt])

  const handleFileChange = useCallback((e) => {
    const file = e.target.files?.[0] || null
    setImageFile(file)
  }, [])

  const handleSubmit = useCallback(async (e) => {
    e.preventDefault()
    setError('')

    if (!wid) {
      setError('缺少 workspace_id，无法创建任务，请从侧边菜单重新进入此页面。')
      return
    }
    if (!prompt.trim()) {
      setError('请填写提示词。')
      return
    }
    if (prompt.length > PROMPT_MAX) {
      setError(`提示词长度不能超过 ${PROMPT_MAX} 字符，目前为 ${prompt.length}。`)
      return
    }
    if (!imageFile) {
      setError('请上传一张 PNG/JPG 图片。')
      return
    }

    setSubmitting(true)
    try {
      const resp = await kieTenantApi.createSora2Task(wid, {
        prompt: prompt.trim(),
        aspect_ratio: aspectRatio,
        n_frames: nFrames,           // service 里会转成字符串
        remove_watermark: removeWatermark,
        image: imageFile,
      })
      setTask(resp?.task || null)
      setFiles(resp?.upload_file ? [resp.upload_file] : [])
    } catch (err) {
      setError(err?.message || '创建任务失败，请稍后再试。')
    } finally {
      setSubmitting(false)
    }
  }, [wid, prompt, aspectRatio, nFrames, removeWatermark, imageFile])

  const handleRefresh = useCallback(async () => {
    if (!wid || !task?.id) return
    setLoadingTask(true)
    setError('')
    try {
      const latest = await kieTenantApi.getSora2Task(wid, task.id, { refresh: true })
      setTask(latest)
      const fs = await kieTenantApi.listSora2TaskFiles(wid, task.id)
      setFiles(fs || [])
    } catch (err) {
      setError(err?.message || '刷新任务状态失败。')
    } finally {
      setLoadingTask(false)
    }
  }, [wid, task])

  return (
    <div>
      <h1 className="page-title">KIE Sora 2 - 图片生成视频</h1>

      {!wid && (
        <div className="alert alert--error" style={{ marginBottom: 16 }}>
          无法识别 workspace_id，请从侧边菜单重新进入本页面。
        </div>
      )}

      <section className="card" style={{ marginBottom: 24 }}>
        <h2 className="card__title">创建任务</h2>

        <form onSubmit={handleSubmit}>
          <FormField label={`提示词（英文建议，最多 ${PROMPT_MAX} 字符）`}>
            <textarea
              className="input"
              rows={10}
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              maxLength={PROMPT_MAX}
              placeholder="请输入英文提示词，描述画面和镜头运镜..."
            />
            <div className="form-field__hint">
              {promptCount} / {PROMPT_MAX}
            </div>
          </FormField>

          <FormField label="画面比例">
            <select
              className="input"
              value={aspectRatio}
              onChange={e => setAspectRatio(e.target.value)}
            >
              {aspectOptions.map(opt => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </FormField>

          <FormField label="视频时长">
            <select
              className="input"
              value={nFrames}
              onChange={e => setNFrames(e.target.value)}
            >
              {frameOptions.map(opt => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </FormField>

          <FormField label="水印">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={removeWatermark}
                onChange={e => setRemoveWatermark(e.target.checked)}
              />
              <span>去除水印（若模型支持）</span>
            </label>
          </FormField>

          <FormField label="源图片（PNG/JPG，≤ 20MB）">
            <input
              type="file"
              accept="image/*"
              onChange={handleFileChange}
            />
            {imageFile && (
              <div className="form-field__hint">
                已选择：{imageFile.name}（{Math.round(imageFile.size / 1024)} KB）
              </div>
            )}
          </FormField>

          {error && (
            <div className="alert alert--error" style={{ marginBottom: 16 }}>
              {error}
            </div>
          )}

          <button
            type="submit"
            className="btn btn--primary"
            disabled={submitting || !wid}
          >
            {submitting ? '创建中...' : '创建任务'}
          </button>
        </form>

        <p className="small-muted" style={{ marginTop: 16 }}>
          说明：提示词尽量具体，包含镜头感、情绪、时长等信息。目前 demo 不保证无违禁内容，
          且你在提示词中描述完整 10s/15s 的镜头脚本效果会更好。
        </p>
      </section>

      {task && (
        <section className="card">
          <h2 className="card__title">任务详情</h2>
          <div className="form-grid">
            <div>任务 ID：</div>
            <div>{task.id}</div>
            <div>KIE TaskId：</div>
            <div>{task.task_id}</div>
            <div>模型：</div>
            <div>{task.model}</div>
            <div>状态：</div>
            <div>{task.state}</div>
          </div>

          <div style={{ marginTop: 12 }}>
            <button
              type="button"
              className="btn"
              onClick={handleRefresh}
              disabled={loadingTask}
            >
              {loadingTask ? '刷新中...' : '刷新状态 & 文件列表'}
            </button>
          </div>

          <h3 style={{ marginTop: 16 }}>相关文件</h3>
          {loadingTask && <Loading />}
          {!loadingTask && files?.length === 0 && (
            <div className="small-muted">暂时还没有文件。</div>
          )}
          {!loadingTask && files?.length > 0 && (
            <ul className="file-list">
              {files.map(f => (
                <li key={f.id}>
                  <div>
                    <strong>{f.kind}</strong>（ID: {f.id}）
                  </div>
                  <div className="small-muted">
                    原始地址：{f.file_url}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  )
}

