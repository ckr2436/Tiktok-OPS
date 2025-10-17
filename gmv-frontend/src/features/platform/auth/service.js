// src/features/platform/auth/service.js
import http from '../../../core/httpClient.js';
import { parseBoolLike } from '../../../utils/booleans.js';

export function parseSessionPayload(p = {}) {
  return {
    id: p.id ?? null,
    email: p.email ?? null,
    username: p.username ?? null,
    display_name: p.display_name ?? null,
    usercode: p.usercode ?? null,
    // 兼容后端不同字段命名
    is_platform_admin: parseBoolLike(p.is_platform_admin ?? p.isPlatformAdmin),
    isPlatformAdmin: parseBoolLike(p.is_platform_admin ?? p.isPlatformAdmin),
    workspace_id: p.workspace_id ?? p.current_workspace_id ?? p.default_workspace_id ?? null,
    role: p.role ?? p.tenantRole ?? null,
    is_active: parseBoolLike(p.is_active ?? true),
  };
}

async function adminExists() {
  const res = await http.get('/platform/admin/exists');
  return res?.data?.exists === true;
}

async function initPlatformOwner({ email, password }) {
  const res = await http.post('/platform/admin/init', { email, password });
  return res?.data ?? {};
}

async function login({ username, password, remember, workspace_id }) {
  // 支持 workspace_id（可空）
  const body = { username, password, remember: parseBoolLike(remember) };
  if (workspace_id) body.workspace_id = Number(workspace_id);
  const res = await http.post('/platform/auth/login', body);
  return parseSessionPayload(res?.data ?? {});
}

async function logout() {
  await http.post('/platform/auth/logout', {});
  return true;
}

async function session() {
  const res = await http.get('/platform/auth/session');
  return parseSessionPayload(res?.data ?? {});
}

// 发现该用户名所在的租户清单（最小信息）
async function discoverTenants(username) {
  if (!username || String(username).trim().length < 2) return [];
  const res = await http.post('/platform/auth/tenants/discover', { username: String(username).trim() });
  const items = Array.isArray(res?.data?.items) ? res.data.items : [];
  // 标准化字段
  return items.map(it => ({
    workspace_id: Number(it.workspace_id),
    company_code: String(it.company_code || ''),
    company_name: String(it.company_name || ''),
  }));
}

const api = { adminExists, initPlatformOwner, login, logout, session, parseSessionPayload, discoverTenants };
export default api;
export { adminExists, initPlatformOwner, login, logout, session, discoverTenants };

