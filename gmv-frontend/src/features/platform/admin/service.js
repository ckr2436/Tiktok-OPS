// src/features/platform/admin/service.js
import http from '../../../core/httpClient'

/** 列出平台管理员 */
export async function listPlatformAdmins({ q = '', page = 1, size = 20 } = {}) {
  // 注意：http 的 baseURL 已是 /api/v1，这里不要再写 /api/v1 前缀
  const res = await http.get('/platform/admin/admins', { params: { q, page, size } })
  return res?.data ?? res
}

/** 删除平台管理员（仅平台 owner 可操作；后端已实现） */
export async function deletePlatformAdmin(userId) {
  const res = await http.delete(`/platform/admin/admins/${userId}`)
  return res?.data ?? res
}

/** 修改展示名（后端已同时支持 PATCH/PUT） */
export async function updatePlatformAdminDisplayName(userId, display_name) {
  const body = { display_name }
  const req = http.patch
    ? http.patch(`/platform/admin/admins/${userId}`, body)
    : http.put(`/platform/admin/admins/${userId}`, body)
  const res = await req
  return res?.data ?? res
}

export async function listPolicyProviders() {
  const res = await http.get('/api/admin/platform/policies/providers', { baseURL: '' })
  return res?.data ?? res
}

export async function listPolicies(params = {}) {
  const query = { ...params }
  const res = await http.get('/api/admin/platform/policies', {
    baseURL: '',
    params: query,
  })
  return res?.data ?? res
}

export async function createPolicy(payload) {
  const res = await http.post('/api/admin/platform/policies', payload, { baseURL: '' })
  return res?.data ?? res
}

export async function updatePolicy(id, payload) {
  const res = await http.patch(`/api/admin/platform/policies/${id}`, payload, { baseURL: '' })
  return res?.data ?? res
}

export async function togglePolicy(id, is_enabled) {
  const res = await http.post(
    `/api/admin/platform/policies/${id}/toggle`,
    { is_enabled },
    { baseURL: '' }
  )
  return res?.data ?? res
}

export async function deletePolicy(id) {
  const res = await http.delete(`/api/admin/platform/policies/${id}`, { baseURL: '' })
  return res?.data ?? res
}

export default {
  listPlatformAdmins,
  deletePlatformAdmin,
  updatePlatformAdminDisplayName,
  listPolicyProviders,
  listPolicies,
  createPolicy,
  updatePolicy,
  togglePolicy,
  deletePolicy,
}

