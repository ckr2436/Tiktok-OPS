// src/features/tenants/openai_whisper/components/ContactSheetResult.jsx
import StatusBadge from './StatusBadge.jsx'
import { apiRoot } from '../../../../core/config.js'

function buildUrl(value) {
  if (!value) return ''
  if (value.startsWith('http')) return value
  return `${apiRoot}${value}`
}

export default function ContactSheetResult({ job }) {
  if (!job || !job.do_contact_sheet) {
    return null
  }

  const status = job.contact_sheet_status || 'pending'
  const downloadUrl = buildUrl(job.contact_sheet_url)

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
          <div style={{ fontWeight: 600, fontSize: 16 }}>拆解视频图片</div>
          <div style={{ fontSize: 12, color: '#6b7280', marginTop: 4 }}>
            抽帧间隔：{job.contact_interval ? `${job.contact_interval}s` : '未知'}
          </div>
        </div>
        <StatusBadge status={status} />
      </div>

      {job.contact_sheet_error ? (
        <div
          style={{
            padding: 12,
            background: '#fef2f2',
            borderRadius: 10,
            color: '#b91c1c',
            border: '1px solid #fecaca',
          }}
        >
          处理失败：{job.contact_sheet_error}
        </div>
      ) : null}

      {status === 'pending' || status === 'processing' ? (
        <div
          style={{
            color: '#6b7280',
            border: '1px dashed #d1d5db',
            padding: 16,
            borderRadius: 12,
          }}
        >
          图片生成中，请稍候…
        </div>
      ) : null}

      {status === 'success' && downloadUrl ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div
            style={{
              borderRadius: 12,
              overflow: 'hidden',
              border: '1px solid #e5e7eb',
              background: '#f9fafb',
            }}
          >
            <img src={downloadUrl} alt="Contact sheet" style={{ width: '100%', display: 'block' }} />
          </div>
          <div>
            <a
              href={downloadUrl}
              target="_blank"
              rel="noreferrer"
              style={{
                padding: '10px 16px',
                borderRadius: 10,
                border: '1px solid #d1d5db',
                background: '#eef2ff',
                textDecoration: 'none',
                color: '#111827',
              }}
            >
              下载 Contact Sheet 图片
            </a>
          </div>
        </div>
      ) : null}
    </div>
  )
}
