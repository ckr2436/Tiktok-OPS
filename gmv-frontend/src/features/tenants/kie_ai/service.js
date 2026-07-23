import http from '../../../core/httpClient.js'

function aiVideoPrefix(wid) {
  if (!wid && wid !== 0) {
    throw new Error('workspace_id (wid) is required')
  }
  return `/tenants/${encodeURIComponent(wid)}/ai-video/videos`
}

function taskPath(wid, taskId, admin = false) {
  const scope = admin ? '/admin' : ''
  return `${aiVideoPrefix(wid)}${scope}/tasks/${encodeURIComponent(taskId)}`
}

async function getProviderStatus(wid) {
  const res = await http.get(`${aiVideoPrefix(wid)}/provider-status`)
  return res?.data ?? res
}

async function createBatch(wid, payload) {
  const res = await http.post(`${aiVideoPrefix(wid)}/batch`, payload, { timeout: 60_000 })
  return res?.data ?? res
}

async function createBatchUpload(wid, { items = [], files = [] } = {}) {
  const form = new FormData()
  form.append('items', JSON.stringify(items))
  for (const file of files) form.append('files', file)
  const res = await http.post(`${aiVideoPrefix(wid)}/batch-upload`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120_000,
  })
  return res?.data ?? res
}

async function createVideo(wid, payload) {
  const res = await http.post(`${aiVideoPrefix(wid)}/generate`, payload, { timeout: 60_000 })
  return res?.data ?? res
}

async function getTask(wid, taskId, { refresh = true, admin = false } = {}) {
  try {
    const res = await http.get(taskPath(wid, taskId, admin), {
      params: { refresh: refresh ? 1 : 0 },
    })
    return res?.data ?? res
  } catch (error) {
    if ((error?.response?.status ?? error?.status) === 404 && !admin) return null
    throw error
  }
}

async function getTaskBatch(wid, taskId) {
  const res = await http.get(`${taskPath(wid, taskId)}/batch`)
  return res?.data ?? res
}

async function listTasks(wid, params = {}, { admin = false } = {}) {
  const scope = admin ? '/admin' : ''
  const res = await http.get(`${aiVideoPrefix(wid)}${scope}/tasks`, { params })
  return res?.data ?? res
}

async function listTaskFiles(wid, taskId, { admin = false } = {}) {
  const res = await http.get(`${taskPath(wid, taskId, admin)}/files`)
  return res?.data ?? res
}

async function getFileDownloadUrl(wid, fileId, { admin = false } = {}) {
  if (!fileId && fileId !== 0) throw new Error('fileId is required')
  const scope = admin ? '/admin' : ''
  const res = await http.get(
    `${aiVideoPrefix(wid)}${scope}/files/${encodeURIComponent(fileId)}/download-url`,
  )
  return res?.data ?? res
}

async function clearTasks(wid, { modelId } = {}) {
  await http.delete(`${aiVideoPrefix(wid)}/tasks`, {
    params: { model: modelId || undefined },
  })
}

async function retryTask(wid, taskId, payload = undefined) {
  const res = await http.post(`${taskPath(wid, taskId)}/retry`, payload)
  return res?.data ?? res
}

async function deleteTask(wid, taskId) {
  const res = await http.delete(taskPath(wid, taskId))
  return res?.data ?? res
}

function filenameFromDisposition(disposition, fallback) {
  const value = String(disposition || '')
  const utf8Match = value.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1])
    } catch {
      return utf8Match[1]
    }
  }
  return value.match(/filename="?([^";]+)"?/i)?.[1] || fallback
}

async function batchDownloadTasks(wid, taskIds = []) {
  const res = await http.post(
    `${aiVideoPrefix(wid)}/tasks/batch-download`,
    { task_ids: taskIds },
    { responseType: 'blob', timeout: 120_000 },
  )
  return {
    blob: res?.data,
    filename: filenameFromDisposition(
      res?.headers?.['content-disposition'],
      'ai-videos.zip',
    ),
  }
}

const aiVideoApi = {
  getProviderStatus,
  createBatch,
  createBatchUpload,
  createVideo,
  getTask,
  getTaskBatch,
  listTasks,
  listTaskFiles,
  getFileDownloadUrl,
  clearTasks,
  retryTask,
  deleteTask,
  batchDownloadTasks,
}

export default aiVideoApi
export {
  getProviderStatus,
  createBatch,
  createBatchUpload,
  createVideo,
  getTask,
  getTaskBatch,
  listTasks,
  listTaskFiles,
  getFileDownloadUrl,
  clearTasks,
  retryTask,
  deleteTask,
  batchDownloadTasks,
}
