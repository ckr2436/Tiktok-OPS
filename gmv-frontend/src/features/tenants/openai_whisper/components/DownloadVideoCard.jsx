// src/features/tenants/openai_whisper/components/DownloadVideoCard.jsx
import StatusBadge from './StatusBadge.jsx'
import { buildDownloadUrl } from '../utils/url.js'

export default function DownloadVideoCard({ job }) {
  const enabled = job && job.download_status && job.download_status !== 'skipped'
  if (!enabled) return null

  const url = buildDownloadUrl(job.download_url)
  const status = job.download_status || 'pending'

  return (
    <div
      style={{
        background: '#fff',
        borderRadius: 16,
        border: '1px solid #e5e7eb',
        padding: 20,
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ fontWeight: 600 }}>原始视频下载</div>
        <StatusBadge status={status} />
      </div>
      {job.download_error ? (
        <div
          style={{
            padding: 12,
            background: '#fef2f2',
            borderRadius: 10,
            color: '#b91c1c',
            border: '1px solid #fecaca',
          }}
        >
          下载失败：{job.download_error}
        </div>
      ) : null}

      {status === 'pending' || status === 'processing' ? (
        <div style={{ color: '#6b7280' }}>视频下载中，请稍候…</div>
      ) : null}

      {status === 'success' && url ? (
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          style={{
            padding: '10px 16px',
            borderRadius: 10,
            border: '1px solid #d1d5db',
            background: '#f9fafb',
            textDecoration: 'none',
            color: '#111827',
            width: 'fit-content',
          }}
        >
          下载视频文件
        </a>
      ) : null}
    </div>
  )
}
