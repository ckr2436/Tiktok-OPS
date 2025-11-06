// src/features/tenants/kie_ai/service.js
import http from '../../../core/httpClient.js'

/**
 * 租户侧 KIE - Sora2 视频生成相关 API
 */

function tenantBase(workspaceId) {
  return `/tenants/${workspaceId}/kie-ai`
}

export async function createSora2Task(workspaceId, payload) {
  const url = `${tenantBase(workspaceId)}/sora2/image-to-video`

  const form = new FormData()
  form.append('prompt', payload.prompt || '')
  form.append('aspect_ratio', payload.aspect_ratio || 'portrait')
  if (payload.n_frames != null) {
    form.append('n_frames', String(payload.n_frames))
  }
  form.append('remove_watermark', String(!!payload.remove_watermark))
  form.append('image', payload.imageFile)

  const res = await http.post(url, form, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  })
  return res.data
}

export async function getSora2Task(workspaceId, taskId, opts = {}) {
  const refresh = opts.refresh ?? true
  const url = `${tenantBase(workspaceId)}/sora2/tasks/${taskId}`
  const res = await http.get(url, { params: { refresh } })
  return res.data
}

export async function listTaskFiles(workspaceId, taskId) {
  const url = `${tenantBase(workspaceId)}/sora2/tasks/${taskId}/files`
  const res = await http.get(url)
  return res.data || []
}

export async function getFileDownloadUrl(workspaceId, fileId) {
  const url = `${tenantBase(workspaceId)}/files/${fileId}/download-url`
  const res = await http.get(url)
  return res.data
}

const kieTenantApi = {
  createSora2Task,
  getSora2Task,
  listTaskFiles,
  getFileDownloadUrl,
}

export default kieTenantApi

