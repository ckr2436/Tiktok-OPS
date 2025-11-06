// src/features/tenants/kie_ai/service.js
import http from '../../../core/httpClient.js'

// 这里 wid 是必填的 workspace_id
const tenantPrefix = (wid) => {
  if (!wid && wid !== 0) {
    throw new Error('workspace_id(wid) is required')
  }
  return `/tenants/${encodeURIComponent(wid)}/kie-ai`
}

// 创建 Sora2 任务：上传图片 + prompt
async function createSora2Task(
  wid,
  { prompt, aspect_ratio, n_frames, remove_watermark, image }
) {
  const form = new FormData()

  form.append('prompt', prompt ?? '')
  form.append('aspect_ratio', aspect_ratio || 'portrait')

  // 后端定义 n_frames 为 10 / 15（秒），这里统一转成字符串
  if (n_frames != null && n_frames !== '') {
    form.append('n_frames', String(n_frames))
  }

  form.append('remove_watermark', remove_watermark ? 'true' : 'false')

  if (image) {
    form.append('image', image)
  }

  const url = `${tenantPrefix(wid)}/sora2/image-to-video`
  const res = await http.post(url, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return res.data
}

// 查询任务
async function getSora2Task(wid, taskId, { refresh = true } = {}) {
  const url = `${tenantPrefix(wid)}/sora2/tasks/${encodeURIComponent(taskId)}`
  const res = await http.get(url, { params: { refresh } })
  return res.data
}

// 查询任务文件列表
async function listSora2TaskFiles(wid, taskId) {
  const url = `${tenantPrefix(wid)}/sora2/tasks/${encodeURIComponent(taskId)}/files`
  const res = await http.get(url)
  return res.data
}

// 获取文件下载 URL
async function getFileDownloadUrl(wid, fileId) {
  const url = `${tenantPrefix(wid)}/files/${encodeURIComponent(fileId)}/download-url`
  const res = await http.get(url)
  return res.data
}

// 默认导出：兼容 import kieTenantApi from '../service.js'
const kieTenantApi = {
  createSora2Task,
  getSora2Task,
  listSora2TaskFiles,
  getFileDownloadUrl,
}

export default kieTenantApi

// 具名导出：如果以后想按需导入也可以
export {
  createSora2Task,
  getSora2Task,
  listSora2TaskFiles,
  getFileDownloadUrl,
}

