// src/features/tenants/openai_whisper/components/StatusBadge.jsx
const STATUS_COLORS = {
  pending: '#9ca3af',
  processing: '#2563eb',
  success: '#16a34a',
  failed: '#dc2626',
}

export default function StatusBadge({ status }) {
  const normalized = String(status || '').toLowerCase()
  const color = STATUS_COLORS[normalized] || '#6b7280'
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '4px 12px',
        borderRadius: 999,
        fontSize: 12,
        background: `${color}22`,
        color,
      }}
    >
      {normalized.toUpperCase() || 'UNKNOWN'}
    </span>
  )
}
