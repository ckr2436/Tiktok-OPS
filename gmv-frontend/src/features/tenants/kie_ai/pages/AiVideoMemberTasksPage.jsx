import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { message } from 'antd'

import { useAppSelector } from '../../../../app/hooks.js'
import Loading from '../../../../components/ui/Loading.jsx'
import Modal from '../../../../components/ui/Modal.jsx'
import { listTenantUsers } from '../../users/service.js'
import kieTenantApi from '../service.js'

const PAGE_SIZE_OPTIONS = [10, 20, 50]
const MODEL_OPTIONS = [
  { value: '', label: '全部模型' },
  { value: 'omni_flash', label: 'Omni Flash' },
  { value: 'seedance_2_0_mini', label: 'Seedance 2.0 Mini' },
]

const STATE_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'waiting', label: 'waiting' },
  { value: 'running', label: 'running' },
  { value: 'success', label: 'success' },
  { value: 'failed', label: 'failed' },
  { value: 'timeout', label: 'timeout' },
]

function statusType(state) {
  const s = String(state || '').toLowerCase()
  if (!s) return 'default'
  if (s.includes('wait') || s.includes('queue')) return 'waiting'
  if (s.includes('run') || s.includes('process') || s.includes('progress') || s.includes('gen')) return 'running'
  if (s === 'success' || s === 'succeeded' || s === 'ok' || s.includes('complete')) return 'success'
  if (s.includes('timeout')) return 'timeout'
  if (s.includes('fail') || s.includes('error')) return 'fail'
  return 'default'
}

function Badge({ type = 'default', children }) {
  const colorMap = {
    waiting: '#999',
    running: '#0d6efd',
    success: '#16a34a',
    fail: '#dc2626',
    timeout: '#f97316',
    default: '#666',
  }
  const color = colorMap[type] || colorMap.default
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '2px 8px',
        borderRadius: 999,
        fontSize: 12,
        color,
        backgroundColor: `${color}22`,
        border: `1px solid ${color}44`,
      }}
    >
      {children}
    </span>
  )
}

function formatCreator(task) {
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

function modelLabel(model) {
  if (['doubao-seedance-2-0-mini-260615', 'seedance_2_0'].includes(model)) return 'Seedance 2.0 Mini'
  return MODEL_OPTIONS.find((item) => item.value === model)?.label || model || '-'
}

export default function AiVideoMemberTasksPage() {
  const { wid } = useParams()
  const session = useAppSelector((s) => s.session?.data || {})
  const role = String(session?.role || '').toLowerCase()
  const isTenantAdmin = role === 'owner' || role === 'admin'

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [model, setModel] = useState('')
  const [state, setState] = useState('')
  const [creatorUserId, setCreatorUserId] = useState('')
  const [selected, setSelected] = useState(null)
  const [preview, setPreview] = useState(null)

  const usersQuery = useQuery({
    queryKey: ['tenant-users-for-ai-video-tasks', wid],
    queryFn: () => listTenantUsers({ wid, page: 1, size: 200 }),
    enabled: !!wid && isTenantAdmin,
    staleTime: 60_000,
  })

  const tasksQuery = useQuery({
    queryKey: [
      'ai-video-member-tasks',
      wid,
      page,
      pageSize,
      model,
      state,
      creatorUserId,
    ],
    queryFn: () =>
      kieTenantApi.listTasks(wid, {
        page,
        size: pageSize,
        model: model || undefined,
        state: state || undefined,
        creator_user_id: creatorUserId || undefined,
        refresh_pending: true,
      }, { admin: true }),
    enabled: !!wid && isTenantAdmin,
    keepPreviousData: true,
  })

  const detailQuery = useQuery({
    queryKey: ['ai-video-member-task-detail', wid, selected?.id],
    queryFn: () => kieTenantApi.getTask(wid, selected.id, { refresh: true, admin: true }),
    enabled: !!wid && !!selected?.id && isTenantAdmin,
  })

  const filesQuery = useQuery({
    queryKey: ['ai-video-member-task-files', wid, selected?.id],
    queryFn: () => kieTenantApi.listTaskFiles(wid, selected.id, { admin: true }),
    enabled: !!wid && !!selected?.id && isTenantAdmin,
  })

  const items = tasksQuery.data?.items || []
  const total = tasksQuery.data?.total || 0
  const totalPages = total ? Math.max(1, Math.ceil(total / pageSize)) : 1
  const users = usersQuery.data?.items || []
  const detail = detailQuery.data || selected
  const files = filesQuery.data || []

  const selectedFilesTitle = useMemo(() => {
    if (!selected?.id) return '成果视频'
    return `任务 #${selected.id} 成果视频`
  }, [selected?.id])

  if (!isTenantAdmin) {
    return (
      <div className="page">
        <div className="card">
          <h2 style={{ marginTop: 0 }}>成员任务记录</h2>
          <div className="alert alert--error">仅公司 owner / 管理员可查看成员任务记录。</div>
        </div>
      </div>
    )
  }

  async function openFile(file, mode) {
    try {
      const url = await kieTenantApi.getFileDownloadUrl(wid, file.id, { admin: true })
      if (mode === 'preview') {
        setPreview({ url, fileId: file.id })
        return
      }
      window.open(url, '_blank', 'noopener,noreferrer')
    } catch (error) {
      message.error(error?.message || '获取文件链接失败')
    }
  }

  return (
    <div className="page">
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
          <div>
            <h2 style={{ margin: 0 }}>成员任务记录</h2>
            <div className="small-muted" style={{ marginTop: 6 }}>
              单独查看成员的 AI 视频任务，不影响你自己的生成页记录。
            </div>
          </div>
          <button type="button" className="btn ghost" onClick={() => tasksQuery.refetch()}>
            {tasksQuery.isFetching ? '刷新中…' : '刷新'}
          </button>
        </div>

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(4, minmax(160px, 1fr))',
            gap: 12,
            marginTop: 16,
          }}
        >
          <label className="small-muted">
            成员
            <select
              className="input"
              value={creatorUserId}
              onChange={(e) => {
                setCreatorUserId(e.target.value)
                setPage(1)
              }}
            >
              <option value="">全部成员</option>
              {users.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.display_name || user.username || user.email}
                  {user.usercode ? `（${user.usercode}）` : ''}
                </option>
              ))}
            </select>
          </label>
          <label className="small-muted">
            模型
            <select
              className="input"
              value={model}
              onChange={(e) => {
                setModel(e.target.value)
                setPage(1)
              }}
            >
              {MODEL_OPTIONS.map((item) => (
                <option key={item.value || 'all'} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="small-muted">
            状态
            <select
              className="input"
              value={state}
              onChange={(e) => {
                setState(e.target.value)
                setPage(1)
              }}
            >
              {STATE_OPTIONS.map((item) => (
                <option key={item.value || 'all'} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="small-muted">
            每页
            <select
              className="input"
              value={pageSize}
              onChange={(e) => {
                setPageSize(Number(e.target.value) || 10)
                setPage(1)
              }}
            >
              {PAGE_SIZE_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {value} 条
                </option>
              ))}
            </select>
          </label>
        </div>

        {tasksQuery.isLoading && <Loading />}

        {!tasksQuery.isLoading && (
          <div className="table-wrapper" style={{ marginTop: 16, maxHeight: 420, overflow: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 80 }}>ID</th>
                  <th style={{ width: 170 }}>创建人</th>
                  <th style={{ width: 180 }}>模型</th>
                  <th style={{ width: 90 }}>状态</th>
                  <th>提示摘要</th>
                  <th style={{ width: 150 }}>创建时间</th>
                  <th style={{ width: 80 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.length === 0 && (
                  <tr>
                    <td colSpan={7} className="small-muted">
                      暂无成员任务记录。
                    </td>
                  </tr>
                )}
                {items.map((task) => (
                  <tr key={task.id}>
                    <td>{task.id}</td>
                    <td className="small-muted">{formatCreator(task)}</td>
                    <td className="small-muted">{modelLabel(task.model)}</td>
                    <td>
                      <Badge type={statusType(task.state)}>{task.state || '-'}</Badge>
                    </td>
                    <td className="small-muted">
                      {(task.prompt || '').slice(0, 52)}
                      {task.prompt && task.prompt.length > 52 ? '…' : ''}
                    </td>
                    <td className="small-muted">
                      {task.created_at ? new Date(task.created_at).toLocaleString() : ''}
                    </td>
                    <td>
                      <button type="button" className="btn ghost" onClick={() => setSelected(task)}>
                        查看
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div
          className="small-muted"
          style={{
            marginTop: 10,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 8,
            flexWrap: 'wrap',
          }}
        >
          <span>共 {total} 条记录</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <button type="button" className="btn ghost sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              上一页
            </button>
            <span>
              第 {page} / {totalPages} 页
            </span>
            <button
              type="button"
              className="btn ghost sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页
            </button>
          </div>
        </div>

        {selected && (
          <section style={{ marginTop: 20, borderTop: '1px solid var(--border)', paddingTop: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
              <h3 style={{ margin: 0 }}>任务详情</h3>
              <button type="button" className="btn ghost" onClick={() => setSelected(null)}>
                关闭详情
              </button>
            </div>
            {detailQuery.isLoading ? (
              <Loading />
            ) : (
              <div style={{ marginTop: 12, display: 'grid', gap: 8 }}>
                <div>
                  <strong>任务 ID：</strong> {detail?.id}
                </div>
                <div>
                  <strong>创建人：</strong> {formatCreator(detail)}
                </div>
                <div>
                  <strong>模型：</strong> {modelLabel(detail?.model)}
                </div>
                <div>
                  <strong>状态：</strong> <Badge type={statusType(detail?.state)}>{detail?.state || '-'}</Badge>
                </div>
                {(detail?.fail_code || detail?.fail_msg) && (
                  <div className="alert alert--error">
                    失败原因：{detail.fail_code ? `[${detail.fail_code}] ` : ''}
                    {detail.fail_msg || '未知错误'}
                  </div>
                )}
                {detail?.prompt && (
                  <details>
                    <summary className="small-muted">查看提示词</summary>
                    <pre style={{ whiteSpace: 'pre-wrap', marginTop: 8 }}>{detail.prompt}</pre>
                  </details>
                )}
              </div>
            )}

            <div style={{ marginTop: 16 }}>
              <h4 style={{ margin: '0 0 8px' }}>{selectedFilesTitle}</h4>
              {filesQuery.isLoading && <Loading />}
              {!filesQuery.isLoading && files.length === 0 && (
                <div className="small-muted">暂无结果文件。</div>
              )}
              {!filesQuery.isLoading && files.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                  {files.map((file) => (
                    <div
                      key={file.id}
                      style={{
                        border: '1px solid var(--border)',
                        borderRadius: 8,
                        padding: 12,
                        minWidth: 220,
                      }}
                    >
                      <div style={{ fontWeight: 600 }}>成果视频</div>
                      <div className="small-muted" style={{ marginTop: 4 }}>
                        {file.kind === 'result_watermark' ? '带水印版本' : '无水印版本'}
                        {file.size_bytes ? ` · ${(Number(file.size_bytes) / 1024 / 1024).toFixed(1)} MB` : ''}
                      </div>
                      <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                        <button type="button" className="btn ghost sm" onClick={() => openFile(file, 'preview')}>
                          播放视频
                        </button>
                        <button type="button" className="btn ghost sm" onClick={() => openFile(file, 'download')}>
                          下载
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>
        )}
      </div>

      {preview && (
        <Modal open title={`任务 #${selected?.id || ''} 视频预览`} onClose={() => setPreview(null)}>
          <video
            key={preview.fileId}
            src={preview.url}
            controls
            autoPlay
            preload="metadata"
            playsInline
            onError={() => message.error('视频加载失败，请点击下载验证文件。')}
            style={{ width: '100%', maxHeight: '72vh', borderRadius: 8 }}
          />
        </Modal>
      )}
    </div>
  )
}
