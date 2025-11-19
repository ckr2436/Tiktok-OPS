// src/features/tenants/openai_whisper/components/SubtitleJobHistory.jsx
import StatusBadge from './StatusBadge.jsx'

function formatDate(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('zh-CN', { hour12: false })
}

export default function SubtitleJobHistory({
  jobs = [],
  selectedJobId,
  onSelect,
  onRefresh,
  loading = false,
  errorMessage = '',
}) {
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
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontWeight: 600 }}>历史任务</div>
          <div style={{ fontSize: 12, color: '#6b7280' }}>最近的识别任务记录，可随时查看状态与结果。</div>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          style={{
            border: '1px solid #d1d5db',
            background: '#f3f4f6',
            borderRadius: 999,
            padding: '6px 14px',
            fontSize: 13,
            cursor: loading ? 'not-allowed' : 'pointer',
          }}
        >
          {loading ? '刷新中…' : '刷新'}
        </button>
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
            return (
              <button
                key={job.job_id}
                type="button"
                onClick={() => onSelect?.(job)}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  width: '100%',
                  border: '1px solid #e5e7eb',
                  borderRadius: 12,
                  padding: 12,
                  background: isActive ? '#eef2ff' : '#fff',
                  cursor: 'pointer',
                  textAlign: 'left',
                  gap: 12,
                }}
              >
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600 }}>{job.filename || job.job_id}</div>
                  <div style={{ fontSize: 12, color: '#6b7280', marginTop: 4 }}>
                    {job.source_language ? job.source_language.toUpperCase() : '自动检测'}
                    {job.translate && job.translation_language
                      ? ` → ${job.translation_language.toUpperCase()}`
                      : ''}
                  </div>
                  <div style={{ fontSize: 12, color: '#9ca3af', marginTop: 4 }}>
                    创建时间：{formatDate(job.created_at) || '未知'}
                  </div>
                </div>
                <StatusBadge status={job.status} />
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
