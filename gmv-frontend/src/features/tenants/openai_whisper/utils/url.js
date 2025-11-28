// src/features/tenants/openai_whisper/utils/url.js
import { apiRoot } from '../../../../core/config.js'
import { safePath } from '../../../../lib/http.js'

export function buildDownloadUrl(value) {
  if (!value) return ''
  if (value.startsWith('http')) return value

  const normalized = value.startsWith('/') ? value : `/${value}`
  if (normalized.startsWith(apiRoot)) {
    return normalized
  }
  return safePath(`${apiRoot}${normalized}`)
}
