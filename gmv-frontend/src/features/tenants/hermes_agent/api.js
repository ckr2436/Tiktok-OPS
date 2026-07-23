import http from '../../../lib/http.js'
import { apiRoot } from '../../../core/config.js'

function basePath(wid) {
  if (!wid) throw new Error('workspace_id (wid) is required')
  return `/tenants/${encodeURIComponent(wid)}/hermes-agent`
}

export async function fetchHermesCapabilities(wid, taskType = 'general') {
  const params = taskType ? { task_type: taskType } : undefined
  const res = await http.get(`${basePath(wid)}/capabilities`, { params })
  return res.data || {}
}

export async function postHermesAgent(wid, endpoint, payload) {
  const res = await http.post(`${basePath(wid)}/${endpoint}`, {
    ...payload,
    async_mode: true,
  })
  return res.data
}

export async function fetchHermesRun(wid, runId) {
  if (!runId) throw new Error('run_id is required')
  const res = await http.get(`${basePath(wid)}/runs/${encodeURIComponent(runId)}`)
  return res.data || {}
}
export async function fetchContentFactoryBridge(wid) {
  const res = await http.get(`${basePath(wid)}/content-factory/bridge`)
  return res.data
}

export async function registerContentFactoryBridge(wid, payload) {
  const res = await http.post(`${basePath(wid)}/content-factory/bridge/register`, payload)
  return res.data
}

export async function bindContentFactoryBridgeDevice(wid, deviceId) {
  const res = await http.post(`${basePath(wid)}/content-factory/bridge/devices/bind`, { device_id: deviceId })
  return res.data
}

export async function selectContentFactoryBridgeDevice(wid, deviceId) {
  const res = await http.post(`${basePath(wid)}/content-factory/bridge/devices/select`, { device_id: deviceId })
  return res.data
}

export async function unbindContentFactoryBridgeDevice(wid, deviceId) {
  const res = await http.delete(`${basePath(wid)}/content-factory/bridge/devices/${encodeURIComponent(deviceId)}`)
  return res.data
}

export async function prepareContentFactoryBridgeSlot(wid, deviceId) {
  const res = await http.post(`${basePath(wid)}/content-factory/bridge/slots`, { device_id: deviceId })
  return res.data
}

export async function removeContentFactoryBridgeSlot(wid, bridgeId) {
  const res = await http.delete(`${basePath(wid)}/content-factory/bridge/slots/${encodeURIComponent(bridgeId)}`)
  return res.data
}

export async function downloadContentFactoryBridgeAgent(wid, { deviceId, deviceName } = {}) {
  const res = await http.get(`${basePath(wid)}/content-factory/bridge/agent/download`, {
    params: { device_id: deviceId, device_name: deviceName || undefined },
    responseType: 'blob',
  })
  return res.data
}

export async function fetchContentFactoryProjects(wid) {
  const res = await http.get(`${basePath(wid)}/content-factory/projects`)
  return res.data?.items || []
}

export async function fetchAdminContentFactoryProjects(wid, params = {}) {
  const res = await http.get(`${basePath(wid)}/content-factory/admin/projects`, { params })
  return res.data?.items || []
}

export async function fetchAdminContentFactoryProject(wid, projectKey) {
  const res = await http.get(`${basePath(wid)}/content-factory/admin/projects/${encodeURIComponent(projectKey)}`)
  return res.data
}

export async function fetchContentFactoryProducts(wid) {
  const res = await http.get(`${basePath(wid)}/content-factory/products`)
  return res.data?.items || []
}

export async function createContentFactoryProduct(wid, payload) {
  const res = await http.post(`${basePath(wid)}/content-factory/products`, payload)
  return res.data
}

export async function updateContentFactoryProduct(wid, productId, payload) {
  const res = await http.patch(`${basePath(wid)}/content-factory/products/${encodeURIComponent(productId)}`, payload)
  return res.data
}

export async function deleteContentFactoryProduct(wid, productId) {
  const res = await http.delete(`${basePath(wid)}/content-factory/products/${encodeURIComponent(productId)}`)
  return res.data
}

export async function uploadContentFactoryProductAssets(wid, productId, files) {
  const body = new FormData()
  Array.from(files || []).forEach((file) => body.append('files', file))
  const res = await http.post(`${basePath(wid)}/content-factory/products/${encodeURIComponent(productId)}/assets`, body)
  return res.data
}

export async function deleteContentFactoryProductAsset(wid, productId, assetId) {
  const res = await http.delete(`${basePath(wid)}/content-factory/products/${encodeURIComponent(productId)}/assets/${encodeURIComponent(assetId)}`)
  return res.data
}

export async function generateContentFactoryProductFacts(wid, productId) {
  const res = await http.post(`${basePath(wid)}/content-factory/products/${encodeURIComponent(productId)}/facts`)
  return res.data
}

export async function createContentFactoryProject(wid, payload) {
  const res = await http.post(`${basePath(wid)}/content-factory/projects`, payload)
  return res.data
}

export async function sendContentFactoryProducerTurn(wid, payload) {
  // Producer turns can legitimately spend several minutes reasoning over a
  // long brief and product packet.  The shared client timeout is intentionally
  // short for ordinary CRUD calls, so this endpoint needs its own budget.
  const res = await http.post(
    `${basePath(wid)}/content-factory/producer/turn`,
    payload,
    { timeout: 195_000 },
  )
  return res.data
}

export async function fetchContentFactoryProducerSession(wid, sessionKey) {
  const res = await http.get(`${basePath(wid)}/content-factory/producer/sessions/${encodeURIComponent(sessionKey)}`)
  return res.data
}

export async function uploadContentFactoryProducerAttachments(
  wid,
  sessionKey,
  files,
  assetKind,
  { characterName, characterDescription } = {},
) {
  const body = new FormData()
  Array.from(files || []).forEach((file) => body.append('files', file))
  body.append('asset_kind', assetKind)
  if (characterName) body.append('character_name', characterName)
  if (characterDescription) body.append('character_description', characterDescription)
  const res = await http.post(
    `${basePath(wid)}/content-factory/producer/sessions/${encodeURIComponent(sessionKey)}/attachments`,
    body,
    { timeout: 240_000 },
  )
  return Array.isArray(res.data) ? res.data : []
}

export async function deleteContentFactoryProducerAttachment(wid, sessionKey, attachmentKey) {
  const res = await http.delete(
    `${basePath(wid)}/content-factory/producer/sessions/${encodeURIComponent(sessionKey)}/attachments/${encodeURIComponent(attachmentKey)}`,
  )
  return res.data
}

export async function confirmContentFactoryProducerProject(wid, sessionKey, proposalSha256) {
  const res = await http.post(
    `${basePath(wid)}/content-factory/producer/sessions/${encodeURIComponent(sessionKey)}/confirm`,
    { proposal_sha256: proposalSha256 },
  )
  return res.data
}

export async function updateContentFactoryProject(wid, projectKey, payload) {
  const res = await http.patch(`${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}`, payload)
  return res.data
}

export async function restartContentFactoryProject(wid, projectKey, { stage, instruction, autoRun = true, browserDeviceId } = {}) {
  const res = await http.post(`${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}/restart`, {
    stage: stage || 'DIRECTOR',
    instruction: instruction || null,
    auto_run: autoRun,
    browser_device_id: browserDeviceId || null,
  })
  return res.data
}

export async function deleteContentFactoryProject(wid, projectKey) {
  const res = await http.delete(`${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}`)
  return res.data
}

export async function pauseContentFactoryProject(wid, projectKey, note) {
  const res = await http.post(`${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}/pause`, { note: note || null })
  return res.data
}

export async function releaseContentFactoryRolloutBatch(
  wid,
  projectKey,
  { authorizedVariantIndices, batchId, pauseWhenComplete = true } = {},
) {
  const res = await http.post(
    `${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}/rollout-gate`,
    {
      authorized_variant_indices: Array.from(authorizedVariantIndices || []),
      batch_id: batchId || null,
      pause_when_complete: Boolean(pauseWhenComplete),
    },
  )
  return res.data
}

export async function resumeContentFactoryProject(wid, projectKey) {
  const res = await http.post(`${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}/resume`)
  return res.data
}

export async function uploadContentFactoryAssets(wid, projectKey, files, assetKind = 'source', metadata = {}) {
  const body = new FormData()
  Array.from(files || []).forEach((file) => body.append('files', file))
  body.append('asset_kind', assetKind)
  if (assetKind === 'character_reference') {
    if (metadata.character_key) body.append('character_key', metadata.character_key)
    if (metadata.character_name) body.append('character_name', metadata.character_name)
    if (metadata.character_description) body.append('character_description', metadata.character_description)
  }
  const res = await http.post(`${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}/assets`, body)
  return res.data
}

export async function runContentFactoryStage(wid, projectKey, { instruction, stage, runMode = 'continue', browserDeviceId } = {}) {
  const res = await http.post(`${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}/run`, {
    instruction: instruction || null,
    stage: stage || null,
    run_mode: runMode,
    browser_device_id: browserDeviceId || null,
  })
  return res.data
}

export function contentFactoryAssetUrl(wid, projectKey, assetId) {
  const path = `${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}/assets/${encodeURIComponent(assetId)}/content`
  return `${String(apiRoot || '').replace(/\/$/, '')}${path}`
}

export function contentFactoryDeliverablesZipUrl(wid, projectKey, kind = 'all') {
  const path = `${basePath(wid)}/content-factory/projects/${encodeURIComponent(projectKey)}/deliverables.zip?kind=${encodeURIComponent(kind || 'all')}`
  return `${String(apiRoot || '').replace(/\/$/, '')}${path}`
}

export function adminContentFactoryAssetUrl(wid, projectKey, assetId) {
  const path = `${basePath(wid)}/content-factory/admin/projects/${encodeURIComponent(projectKey)}/assets/${encodeURIComponent(assetId)}/content`
  return `${String(apiRoot || '').replace(/\/$/, '')}${path}`
}

export function adminContentFactoryDeliverablesZipUrl(wid, projectKey, kind = 'all') {
  const path = `${basePath(wid)}/content-factory/admin/projects/${encodeURIComponent(projectKey)}/deliverables.zip?kind=${encodeURIComponent(kind || 'all')}`
  return `${String(apiRoot || '').replace(/\/$/, '')}${path}`
}

export function contentFactoryProductAssetUrl(wid, productId, assetId) {
  const path = `${basePath(wid)}/content-factory/products/${encodeURIComponent(productId)}/assets/${encodeURIComponent(assetId)}/content`
  return `${String(apiRoot || '').replace(/\/$/, '')}${path}`
}
