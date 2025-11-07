// src/features/tenants/kie_ai/service.js
import http from '../../../core/httpClient.js'

// wid = workspace_id
function tenantPrefix (wid) {
  if (!wid && wid !== 0) {
    throw new Error('workspace_id (wid) is required')
  }
  return `/tenants/${encodeURIComponent(wid)}/kie-ai/sora2`
}

// 创建 Sora2 任务：上传图片 + prompt（走 Celery）
async function createImageToVideoTask (
  wid,
  { prompt, aspect_ratio, n_frames, remove_watermark, image }
) {
  const form = new FormData()
  form.append('prompt', prompt ?? '')
  form.append('aspect_ratio', aspect_ratio || 'portrait')

  if (n_frames != null && n_frames !== '') {
    form.append('n_frames', String(n_frames))
  }

  form.append('remove_watermark', remove_watermark ? 'true' : 'false')

  if (image) {
    form.append('image', image)
  }

  const url = `${tenantPrefix(wid)}/image-to-video`
  const res = await http.post(url, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    // 上传 + 调 KIE 比较慢，单独把超时时间拉长一点
    timeout: 60_000,
  })
  return res?.data ?? res
}

/**
 * 获取单个任务详情
 * GET /tenants/{wid}/kie-ai/sora2/tasks/{id}?refresh=1
 */
async function getTask (wid, taskId, { refresh = true } = {}) {
  const url = `${tenantPrefix(wid)}/tasks/${encodeURIComponent(taskId)}`
  const res = await http.get(url, { params: { refresh: refresh ? 1 : 0 } })
  return res?.data ?? res
}

/**
 * 任务历史列表
 * GET /tenants/{wid}/kie-ai/sora2/tasks?page=&size=&state=
 * （后端按你那边实现，这里只消费 items/total）
 */
async function listTasks (wid, params = {}) {
  const url = `${tenantPrefix(wid)}/tasks`
  const res = await http.get(url, { params })
  return res?.data ?? res
}

/**
 * 某任务的文件列表
 * GET /tenants/{wid}/kie-ai/sora2/tasks/{id}/files
 */
async function listTaskFiles (wid, taskId) {
  const url = `${tenantPrefix(wid)}/tasks/${encodeURIComponent(taskId)}/files`
  const res = await http.get(url)
  return res?.data ?? res
}

/**
 * 获取文件下载 URL
 * GET /tenants/{wid}/kie-ai/files/{fileId}/download-url
 */
async function getFileDownloadUrl (wid, fileId) {
  const url = `/tenants/${encodeURIComponent(wid)}/kie-ai/files/${encodeURIComponent(fileId)}/download-url`
  const res = await http.get(url)
  return res?.data ?? res
}

const kieTenantApi = {
  createImageToVideoTask,
  getTask,
  listTasks,
  listTaskFiles,
  getFileDownloadUrl,
}

export default kieTenantApi
export {
  createImageToVideoTask,
  getTask,
  listTasks,
  listTaskFiles,
  getFileDownloadUrl,
}

