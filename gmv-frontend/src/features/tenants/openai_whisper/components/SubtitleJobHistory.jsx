// src/features/tenants/openai_whisper/components/SubtitleJobHistory.jsx
import StatusBadge from './StatusBadge.jsx'

function formatDate(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('zh-CN', { hour12: false })
}

const ACTIVE_STATUSES = new Set(['pending', 'processing'])

const ACTION_BUTTON = {
  border: '1px solid #d1d5db',
  background: '#f3f4f6',
  borderRadius: 999,
  padding: '6px 14px',
  fontSize: 13,
}

export default function SubtitleJobHistory({
  jobs = [],
  selectedJobId,
  onSelect,
  onRefresh,
  onDelete,
  onClear,
  loading = false,
  deletingJobId = '',
  clearing = false,
  errorMessage = '',
}) {
  const busy = loading || clearing || !!deletingJobId
  return (
    <div
      style={{
        background: '#fff',
        borderRadius: 16,
        border: '1px solid #e5e7eb',
        padding: 20,
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
        <div>
          <div style={{ fontWeight: 600 }}>历史任务</div>
          <div style={{ fontSize: 12, color: '#6b7280' }}>最近的识别任务记录，可随时查看状态与结果。</div>
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          <button
            type="button"
            onClick={onRefresh}
            disabled={busy}
            style={{
              ...ACTION_BUTTON,
              cursor: busy ? 'not-allowed' : 'pointer',
            }}
          >
            {loading ? '刷新中…' : '刷新'}
          </button>
          <button
            type="button"
            onClick={() => onClear?.('failed')}
            disabled={busy || jobs.length === 0}
            style={{
              ...ACTION_BUTTON,
              background: '#fff7ed',
              borderColor: '#fed7aa',
              color: '#c2410c',
              cursor: busy || jobs.length === 0 ? 'not-allowed' : 'pointer',
            }}
          >
            清空失败
          </button>
          <button
            type="button"
            onClick={() => onClear?.('terminal')}
            disabled={busy || jobs.length === 0}
            style={{
              ...ACTION_BUTTON,
              background: '#fef2f2',
              borderColor: '#fecaca',
              color: '#dc2626',
              cursor: busy || jobs.length === 0 ? 'not-allowed' : 'pointer',
            }}
          >
            清空已完成
          </button>
        </div>
      </div>
      {errorMessage ? (
        <div
          style={{
            color: '#b91c1c',
            background: '#fef2f2',
            border: '1px solid #fecaca',
            borderRadius: 10,
            padding: '10px 12px',
            fontSize: 13,
          }}
        >
          {errorMessage}
        </div>
      ) : null}
      {jobs.length === 0 ? (
        <div
          style={{
            color: '#6b7280',
            border: '1px dashed #d1d5db',
            borderRadius: 12,
            padding: 16,
            textAlign: 'center',
            fontSize: 14,
          }}
        >
          还没有历史任务，上传视频并开始识别吧。
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {jobs.map((job) => {
            const isActive = job.job_id === selectedJobId
            const isActiveStatus = ACTIVE_STATUSES.has(String(job.status || '').toLowerCase())
            const isDeleting = deletingJobId === job.job_id
            const deleteLabel = isActiveStatus ? '强制删除' : '删除'
            return (
              <div
                key={job.job_id}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  width: '100%',
                  border: '1px solid #e5e7eb',
                  borderRadius: 12,
                  padding: 12,
                  background: isActive ? '#eef2ff' : '#fff',
                  gap: 12,
                }}
              >
                <button
                  type="button"
                  onClick={() => onSelect?.(job)}
                  style={{
                    flex: 1,
                    border: 'none',
                    background: 'transparent',
                    cursor: 'pointer',
                    textAlign: 'left',
                    padding: 0,
                    minWidth: 0,
                  }}
                >
                  <div style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {job.filename || job.job_id}
                  </div>
                  <div style={{ fontSize: 12, color: '#6b7280', marginTop: 4 }}>
                    {job.source_language ? job.source_language.toUpperCase() : '自动检测'}
                    {job.translate && job.translation_language ? ` → ${job.translation_language.toUpperCase()}` : ''}
                  </div>
                  <div style={{ fontSize: 12, color: '#9ca3af', marginTop: 4 }}>
                    创建时间：{formatDate(job.created_at) || '未知'}
                  </div>
                </button>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
                  <StatusBadge status={job.status} />
                  <button
                    type="button"
                    onClick={() => onDelete?.(job, { force: isActiveStatus })}
                    disabled={busy}
                    title={isActiveStatus ? '任务卡在处理中时可强制删除记录和文件' : '删除该任务'}
                    style={{
                      border: '1px solid #fecaca',
                      background: isActiveStatus ? '#fff7ed' : '#fff1f2',
                      color: isActiveStatus ? '#c2410c' : '#e11d48',
                      borderRadius: 999,
                      padding: '5px 10px',
                      fontSize: 12,
                      cursor: busy ? 'not-allowed' : 'pointer',
                      opacity: busy ? 0.55 : 1,
                    }}
                  >
                    {isDeleting ? '删除中…' : deleteLabel}
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
