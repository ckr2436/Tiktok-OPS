// src/features/tenants/integrations/tiktok_business/service.js

const prefix = (wid) =>
  `/api/v1/tenants/${encodeURIComponent(wid)}/oauth/tiktok-business`;

/* ---------- 公司信息 ---------- */
export async function getTenantMeta(wid) {
  const r = await fetch(`/api/v1/tenants/${encodeURIComponent(wid)}/meta`, {
    credentials: 'include',
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json(); // { id, name, company_code }
}

/* ---------- Provider Apps / 授权会话 ---------- */

/** GET /provider-apps -> { items: [...] } */
export async function listProviderApps(wid) {
  const r = await fetch(`${prefix(wid)}/provider-apps`, { credentials: 'include' });
  if (!r.ok) throw new Error(await r.text());
  const data = await r.json();
  return Array.isArray(data?.items) ? data.items : [];
}

/** POST /authz -> { state, auth_url, expires_at } */
export async function createAuthz(wid, { provider_app_id, alias, return_to }) {
  const r = await fetch(`${prefix(wid)}/authz`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({
      provider_app_id,
      alias: alias ?? null,        // 前端叫“名称”，后端字段是 alias
      return_to: return_to ?? null,
    }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/* ---------- 绑定列表 ---------- */

/** GET /bindings -> { items: [...] } */
export async function listBindings(wid) {
  const r = await fetch(`${prefix(wid)}/bindings`, { credentials: 'include' });
  if (!r.ok) throw new Error(await r.text());
  const data = await r.json();
  return Array.isArray(data?.items) ? data.items : [];
}

/** 详情页没有独立接口，这里前端通过列表过滤 */
export async function getBindingById(wid, auth_id) {
  const list = await listBindings(wid);
  return list.find((x) => String(x.auth_id) === String(auth_id)) || null;
}

/* ---------- 撤销 / 删除 ---------- */

/** POST /bindings/{auth_id}/revoke?remote=true -> { removed_advertisers } */
export async function revokeBinding(wid, auth_id, remote = true) {
  const r = await fetch(
    `${prefix(wid)}/bindings/${encodeURIComponent(auth_id)}/revoke?remote=${remote ? 'true' : 'false'}`,
    { method: 'POST', credentials: 'include' }
  );
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** DELETE /bindings/{auth_id} -> { removed_advertisers, removed_accounts } */
export async function hardDeleteBinding(wid, auth_id) {
  const r = await fetch(`${prefix(wid)}/bindings/${encodeURIComponent(auth_id)}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/* ---------- 广告主相关（只读 + 设置主） ---------- */

/** GET /bindings/{auth_id}/advertisers -> [ ... ] */
export async function advertisersOf(wid, auth_id) {
  const r = await fetch(`${prefix(wid)}/bindings/${encodeURIComponent(auth_id)}/advertisers`, {
    credentials: 'include',
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** POST /bindings/{auth_id}/set-primary -> { count } */
export async function setPrimary(wid, auth_id, advertiser_id) {
  const r = await fetch(`${prefix(wid)}/bindings/${encodeURIComponent(auth_id)}/set-primary`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ advertiser_id }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** PATCH /bindings/{auth_id}/alias -> { auth_id, alias } */
export async function updateAlias(wid, auth_id, alias) {
  const r = await fetch(`${prefix(wid)}/bindings/${encodeURIComponent(auth_id)}/alias`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ alias: alias ?? null }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

