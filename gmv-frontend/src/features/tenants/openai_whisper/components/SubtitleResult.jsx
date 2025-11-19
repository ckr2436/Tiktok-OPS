// src/features/tenants/openai_whisper/components/SubtitleResult.jsx
import StatusBadge from './StatusBadge.jsx'
import { formatTimeRange } from '../utils/formatters.js'

export default function SubtitleResult({ job }) {
  if (!job) {
    return (
      <div
        style={{
          padding: 24,
          background: '#fff',
          borderRadius: 16,
          border: '1px solid #e5e7eb',
          textAlign: 'center',
          color: '#6b7280',
        }}
      >
        上传视频并提交任务后将在这里显示识别结果。
      </div>
    )
  }

  const segments = Array.isArray(job.segments) ? job.segments : []
  const translationSegments = Array.isArray(job.translation_segments)
    ? job.translation_segments
    : []

  const translationReady = job.translate && translationSegments.length > 0
  const showSource = !job.translate || job.show_bilingual || !translationReady
  const showTranslation = translationReady

  const rows = showSource ? segments : translationSegments
  const translationMap = new Map(
    translationSegments.map((seg, index) => [seg.index ?? index, seg]),
  )

  return (
    <div
      style={{
        background: '#fff',
        borderRadius: 16,
        border: '1px solid #e5e7eb',
        padding: 24,
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: 18, fontWeight: 600 }}>字幕识别结果</div>
          <div style={{ fontSize: 13, color: '#6b7280', marginTop: 4 }}>
            检测语言：{job.detected_language || '自动'}
            {job.translation_language ? ` ｜ 翻译语言：${job.translation_language}` : ''}
          </div>
        </div>
        <StatusBadge status={job.status} />
      </div>

      {job.error ? (
        <div
          style={{
            padding: 16,
            background: '#fef2f2',
            borderRadius: 12,
            color: '#b91c1c',
            border: '1px solid #fecaca',
          }}
        >
          处理失败：{job.error}
        </div>
      ) : null}

      <div
        style={{
          maxHeight: 480,
          overflowY: 'auto',
          border: '1px solid #f3f4f6',
          borderRadius: 12,
          padding: 16,
          background: '#f9fafb',
        }}
      >
        {rows.length === 0 ? (
          <p style={{ color: '#6b7280', textAlign: 'center' }}>暂无可展示的字幕内容。</p>
        ) : (
          rows.map((seg, idx) => {
            const translation = translationMap.get(seg.index ?? idx)
            return (
              <div
                key={`${seg.index ?? idx}-${idx}`}
                style={{
                  padding: '12px 0',
                  borderBottom: '1px solid #e5e7eb',
                }}
              >
                <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 6 }}>
                  {formatTimeRange(seg.start, seg.end)}
                </div>
                {showSource && (
                  <p style={{ margin: 0, fontSize: 16, fontWeight: 500 }}>{seg.text}</p>
                )}
                {showTranslation && (!showSource || job.show_bilingual) && translation ? (
                  <p
                    style={{
                      margin: '6px 0 0',
                      fontSize: 15,
                      color: job.show_bilingual ? '#2563eb' : '#111827',
                    }}
                  >
                    {translation.text}
                  </p>
                ) : null}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}

