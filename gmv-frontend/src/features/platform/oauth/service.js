// src/features/platform/oauth/service.js
import http from '../../../core/httpClient'

// ---------- 工具 ----------
function trimOrNull(v) {
  if (v === undefined || v === null) return null
  const s = String(v).trim()
  return s.length ? s : null
}

// 列表：GET /api/v1/platform/oauth/provider-apps
export async function listProviderApps() {
  const res = await http.get('/platform/oauth/provider-apps')
  // 后端直接返回数组；若未来改为 {items:[]} 也容错
  const raw = Array.isArray(res?.data) ? res.data : (res?.data?.items ?? [])
  // 统一整理字段，确保前端渲染不因后端微调而崩
  return (raw || []).map(it => ({
    id: Number(it?.id),
    provider: String(it?.provider ?? 'tiktok-business'),
    name: String(it?.name ?? ''),
    client_id: String(it?.client_id ?? ''), // ★ 新字段名
    redirect_uri: String(it?.redirect_uri ?? ''),
    is_enabled: !!it?.is_enabled,
    client_secret_key_version: Number(it?.client_secret_key_version ?? 0),
    updated_at: it?.updated_at ?? null,
  }))
}

// 新建/更新：POST /api/v1/platform/oauth/provider-apps
// 后端同一路由 upsert（创建时必须带 client_secret；更新允许 client_secret 为空/null）
export async function upsertProviderApp(payload) {
  const body = {
    provider: 'tiktok-business',
    name: String(payload.name || '').trim(),
    client_id: String(payload.client_id || '').trim(),
    client_secret: payload.client_secret !== undefined
      ? trimOrNull(payload.client_secret)
      : null, // 编辑时可显式传 null 表示不变
    redirect_uri: String(payload.redirect_uri || '').trim(),
    is_enabled: !!payload.is_enabled,
  }

  // 基础校验（前端兜底；后端仍会再次校验）
  if (!body.name || body.name.length < 2) {
    throw new Error('Name 长度至少 2。')
  }
  if (!body.client_id || body.client_id.length < 4) {
    throw new Error('Client ID 长度至少 4。')
  }
  if (!/^https?:\/\/.+/i.test(body.redirect_uri)) {
    throw new Error('Redirect URI 必须是以 http/https 开头的有效 URL。')
  }

  const res = await http.post('/platform/oauth/provider-apps', body)
  return res?.data ?? res
}

export default {
  listProviderApps,
  upsertProviderApp,
}

