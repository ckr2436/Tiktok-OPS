// src/features/tenants/openai_whisper/utils/formatters.js
export function formatTime(seconds) {
  if (Number.isNaN(seconds) || !Number.isFinite(seconds)) return '00:00:00.000'
  const totalMs = Math.max(0, Math.round(seconds * 1000))
  const hours = Math.floor(totalMs / 3600000)
  const minutes = Math.floor((totalMs % 3600000) / 60000)
  const secs = Math.floor((totalMs % 60000) / 1000)
  const ms = totalMs % 1000
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${String(ms).padStart(3, '0')}`
}

export function formatTimeRange(start, end) {
  return `${formatTime(start)} - ${formatTime(end)}`
}

