// src/features/platform/tenants/service.js
import http from '../../../core/httpClient'

/** 列出公司（工作区） */
export async function listCompanies({ q = '', page = 1, size = 20 } = {}) {
  // http 的 baseURL 已是 /api/v1，这里不要再写 /api/v1
  const res = await http.get('/platform/companies', { params: { q, page, size } })
  return res?.data ?? res
}

/** 创建公司，并创建 owner（后台请求体与返回值见后端 router_companies.py） */
export async function createCompany(payload) {
  const res = await http.post('/platform/companies', payload)
  return res?.data ?? res
}

/** 删除公司（仅平台 owner，可删除非 0000 的公司） */
export async function deleteCompany(workspaceId) {
  const res = await http.delete(`/platform/companies/${workspaceId}`)
  return res?.data ?? res
}

export default {
  listCompanies,
  createCompany,
  deleteCompany,
}

