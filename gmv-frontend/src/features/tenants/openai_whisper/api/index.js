// src/features/tenants/openai_whisper/api/index.js
import http from '../../../../lib/http.js'
import { apiRoot } from '../../../../core/config.js'

function basePath(wid) {
  return `/tenants/${encodeURIComponent(wid)}/openai-whisper`
}

export async function fetchLanguages(wid) {
  const res = await http.get(`${basePath(wid)}/languages`)
  return res.data?.languages ?? []
}

export async function createSubtitleJob(wid, payload, options = {}) {
  const form = new FormData()
  form.append('file', payload.file)
  if (payload.sourceLanguage) {
    form.append('source_language', payload.sourceLanguage)
  }
  form.append('translate', payload.translate ? 'true' : 'false')
  if (payload.translate && payload.targetLanguage) {
    form.append('target_language', payload.targetLanguage)
  }
  form.append('show_bilingual', payload.showBilingual ? 'true' : 'false')

  const config = {}
  if (typeof options.onUploadProgress === 'function') {
    config.onUploadProgress = options.onUploadProgress
  }

  const res = await http.post(`${basePath(wid)}/jobs`, form, config)
  return res.data
}

export async function fetchSubtitleJob(wid, jobId) {
  if (!jobId) return null
  const res = await http.get(`${basePath(wid)}/jobs/${encodeURIComponent(jobId)}`)
  return res.data
}

export function buildSubtitleDownloadUrl(wid, jobId, variant = 'source') {
  const safeVariant = variant === 'translation' ? 'translation' : 'source'
  return `${apiRoot}${basePath(wid)}/jobs/${encodeURIComponent(jobId)}/subtitles?variant=${safeVariant}`
}

