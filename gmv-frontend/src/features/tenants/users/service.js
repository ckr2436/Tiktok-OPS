// src/features/tenants/users/service.js
import http from '../../../core/httpClient'

/** 列表 */
export async function listTenantUsers({ wid, q = '', page = 1, size = 20 }) {
  const res = await http.get(`/tenants/${wid}/users`, { params: { q, page, size } })
  return res?.data ?? res
}

/** 元信息（公司名） */
export async function getTenantMeta(wid) {
  const res = await http.get(`/tenants/${wid}/meta`)
  return res?.data ?? res
}

/** 获取单个成员 */
export async function getTenantUser(wid, userId) {
  const res = await http.get(`/tenants/${wid}/users/${userId}`)
  return res?.data ?? res
}

/** 新建（role: admin|member） */
export async function createTenantUser(wid, payload) {
  const res = await http.post(`/tenants/${wid}/users`, payload)
  return res?.data ?? res
}

/** 更新（PATCH 优先，PUT 兜底） */
export async function updateTenantUser(wid, userId, patch) {
  let res
  if (typeof http.patch === 'function') {
    res = await http.patch(`/tenants/${wid}/users/${userId}`, patch)
  } else {
    res = await http.put(`/tenants/${wid}/users/${userId}`, patch)
  }
  return res?.data ?? res
}

/** 删除 */
export async function deleteTenantUser(wid, userId) {
  const res = await http.delete(`/tenants/${wid}/users/${userId}`)
  return res?.data ?? res
}

/** 重置密码 */
export async function resetTenantUserPassword(wid, userId, new_password) {
  const res = await http.post(`/tenants/${wid}/users/${userId}/reset_password`, { new_password })
  return res?.data ?? res
}

export default {
  listTenantUsers,
  getTenantMeta,
  getTenantUser,
  createTenantUser,
  updateTenantUser,
  deleteTenantUser,
  resetTenantUserPassword,
}

