// src/features/tenants/kie_ai/service.js
import http from '../../../core/httpClient.js'

// wid = workspace_id
function tenantPrefix(wid) {
  if (!wid && wid !== 0) {
    throw new Error('workspace_id (wid) is required')
  }
  return `/tenants/${encodeURIComponent(wid)}/kie-ai/sora2`
}

function pathForModel(wid, modelId) {
  const base = tenantPrefix(wid)
  switch (modelId) {
    case 'sora-2-text-to-video':
      return `${base}/text-to-video`
    case 'sora-2-pro-text-to-video':
      return `${base}/pro-text-to-video`
    case 'sora-2-image-to-video':
      return `${base}/image-to-video`
    case 'sora-2-pro-image-to-video':
      return `${base}/pro-image-to-video`
    case 'sora-2-pro-storyboard':
      return `${base}/pro-storyboard`
    case 'sora-watermark-remover':
      return `${base}/watermark-remover`
    default:
      throw new Error(`Unsupported Sora2 model: ${modelId}`)
  }
}

// 通用创建 Sora2 任务
async function createSora2Task(
  wid,
  {
    modelId,
    prompt,
    aspect_ratio,
    n_frames,
    remove_watermark,
    size,
    image,
    video_url,
    shots,
  },
) {
  if (!modelId) {
    throw new Error('modelId is required for createSora2Task')
  }
  const url = pathForModel(wid, modelId)

  const form = new FormData()
  if (prompt != null && prompt !== '') {
    form.append('prompt', prompt)
  }
  if (aspect_ratio) {
    form.append('aspect_ratio', aspect_ratio)
  }
  if (n_frames != null && n_frames !== '') {
    form.append('n_frames', String(n_frames))
  }
  if (typeof remove_watermark === 'boolean') {
    form.append('remove_watermark', remove_watermark ? 'true' : 'false')
  }
  if (size) {
    form.append('size', size)
  }
  if (video_url) {
    form.append('video_url', video_url)
  }
  if (shots && Array.isArray(shots) && shots.length > 0) {
    form.append('shots', JSON.stringify(shots))
  }
  if (image) {
    form.append('image', image)
  }

  const res = await http.post(url, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60_000,
  })
  return res?.data ?? res
}

// 兼容用法：只创建标准版 image-to-video（旧页面如果还在用的话）
async function createImageToVideoTask(
  wid,
  { prompt, aspect_ratio, n_frames, remove_watermark, image },
) {
  return createSora2Task(wid, {
    modelId: 'sora-2-image-to-video',
    prompt,
    aspect_ratio,
    n_frames,
    remove_watermark,
    image,
  })
}

/**
 * 获取单个任务详情
 * GET /tenants/{wid}/kie-ai/sora2/tasks/{id}?refresh=1
 *
 * - 正常返回任务对象
 * - 如果后端 404，则返回 null（不抛错），用于前端自动清理当前任务
 */
async function getTask(wid, taskId, { refresh = true } = {}) {
  const url = `${tenantPrefix(wid)}/tasks/${encodeURIComponent(taskId)}`
  try {
    const res = await http.get(url, { params: { refresh: refresh ? 1 : 0 } })
    return res?.data ?? res
  } catch (err) {
    const status = err?.response?.status ?? err?.status
    if (status === 404) {
      // 任务不存在，返回 null，交给前端去清理状态
      return null
    }
    throw err
  }
}

/**
 * 任务历史列表
 * GET /tenants/{wid}/kie-ai/sora2/tasks?page=&size=&state=&model=
 */
async function listTasks(wid, params = {}) {
  const url = `${tenantPrefix(wid)}/tasks`
  const res = await http.get(url, { params })
  return res?.data ?? res
}

/**
 * 某任务的文件列表
 * GET /tenants/{wid}/kie-ai/sora2/tasks/{id}/files
 */
async function listTaskFiles(wid, taskId) {
  const url = `${tenantPrefix(wid)}/tasks/${encodeURIComponent(taskId)}/files`
  const res = await http.get(url)
  return res?.data ?? res
}

/**
 * 获取文件下载 URL
 * GET /tenants/{wid}/kie-ai/sora2/files/{fileId}/download-url
 * 返回值是后端从 KIE 刷新的真实下载链接字符串
 */
async function getFileDownloadUrl(wid, fileId) {
  if (!wid && wid !== 0) {
    throw new Error('workspace_id (wid) is required')
  }
  if (!fileId && fileId !== 0) {
    throw new Error('fileId is required')
  }

  const url = `${tenantPrefix(wid)}/files/${encodeURIComponent(fileId)}/download-url`
  const res = await http.get(url)
  // 后端直接返回 string，所以这里直接透传
  return res?.data ?? res
}

/**
 * 清空某个模型的任务记录（及其关联文件）
 * DELETE /tenants/{wid}/kie-ai/sora2/tasks?model=
 */
async function clearTasks(wid, { modelId } = {}) {
  if (!wid && wid !== 0) {
    throw new Error('workspace_id (wid) is required')
  }
  const url = `${tenantPrefix(wid)}/tasks`
  await http.delete(url, {
    params: {
      model: modelId || undefined,
    },
  })
}

const kieTenantApi = {
  createSora2Task,
  createImageToVideoTask,
  getTask,
  listTasks,
  listTaskFiles,
  getFileDownloadUrl,
  clearTasks,
}

export default kieTenantApi
export {
  createSora2Task,
  createImageToVideoTask,
  getTask,
  listTasks,
  listTaskFiles,
  getFileDownloadUrl,
  clearTasks,
}

