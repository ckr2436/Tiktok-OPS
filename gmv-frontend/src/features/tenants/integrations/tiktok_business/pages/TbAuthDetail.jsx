// src/features/tenants/integrations/tiktok_business/pages/TbAuthDetail.jsx
import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  listBindings,
  advertisersOf,
  setPrimary,
  updateAlias,
  revokeBinding,
  hardDeleteBinding,
  createAuthz,
} from '../service.js';

/* 英文状态 -> 中文 */
function cnStatus(s) {
  const v = String(s || '').toLowerCase();
  if (v === 'active') return '已授权';
  if (v === 'revoked') return '已撤销';
  if (v === 'inactive') return '已冻结';
  if (v === 'expired') return '已过期';
  return '未知';
}
function fmt(dt) {
  try {
    if (!dt) return '-';
    const d = new Date(dt);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  } catch { return String(dt || '-'); }
}

export default function TbAuthDetail() {
  const { wid, auth_id } = useParams();
  const nav = useNavigate();

  const [binding, setBinding] = useState(null);
  const [advertisers, setAdvertisers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);

  const [editing, setEditing] = useState(false);
  const [nameInput, setNameInput] = useState('');
  const [saving, setSaving] = useState(false);

  const primaryId = useMemo(
    () => (advertisers.find((a) => a.primary_flag) || {}).advertiser_id,
    [advertisers]
  );

  async function loadAll() {
    setLoading(true);
    setErr(null);
    try {
      // 后端没有「按 id 取详情」接口，这里从列表中过滤
      const list = await listBindings(wid);
      const b = list.find((x) => String(x.auth_id) === String(auth_id));
      if (!b) {
        setErr('未找到该授权记录');
        setLoading(false);
        return;
      }
      setBinding(b);
      setNameInput(b.alias || '');
      const ads = await advertisersOf(wid, auth_id);
      setAdvertisers(Array.isArray(ads) ? ads : []);
    } catch (e) {
      setErr(e?.message || '加载失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadAll(); /* eslint-disable-next-line */ }, [wid, auth_id]);

  if (loading) return <div className="p-6">加载中…</div>;
  if (err) {
    return (
      <div className="p-6 space-y-3">
        <div className="text-red-500">错误：{String(err)}</div>
        <button className="btn ghost" onClick={() => nav(-1)}>返回</button>
      </div>
    );
  }

  // 操作：冻结/激活/重新授权/移除
  async function onFreezeOrActivate() {
    if (String(binding.status).toLowerCase() === 'active') {
      if (!confirm('确定要冻结（撤销长期令牌）吗？')) return;
      await revokeBinding(wid, binding.auth_id, true);
      await loadAll();
    } else {
      // 激活 = 重新授权
      const return_to = `${window.location.origin}/tenants/${encodeURIComponent(wid)}/tiktok_business`;
      await createAuthz(wid, {
        provider_app_id: binding.provider_app_id,
        alias: binding.alias || null,
        return_to,
      }).then(({ auth_url }) => {
        window.open(auth_url, '_blank', 'noopener,noreferrer');
      });
    }
  }
  async function onReauth() {
    const return_to = `${window.location.origin}/tenants/${encodeURIComponent(wid)}/tiktok_business`;
    const { auth_url } = await createAuthz(wid, {
      provider_app_id: binding.provider_app_id,
      alias: binding.alias || null,
      return_to,
    });
    window.open(auth_url, '_blank', 'noopener,noreferrer');
  }
  async function onRemove() {
    if (!confirm('确定要移除该授权记录吗？此操作会直接删除本地记录。')) return;
    await hardDeleteBinding(wid, binding.auth_id);
    nav(`/tenants/${wid}/tiktok_business`);
  }

  return (
    <div className="p-4 md:p-6 space-y-12">
      {/* 面包屑 + 标题 */}
      <div className="card">
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="small-muted">
              <Link to={`/tenants/${wid}/tiktok_business`} className="hover:underline">← 返回列表</Link>
            </div>
            <div className="text-xl font-semibold mt-1">TikTok Business 授权 · 详情</div>
            <div className="small-muted mt-1">
              状态：{cnStatus(binding.status)}　·　授权时间：{fmt(binding.created_at)}
            </div>
          </div>
          <div className="flex items-center gap-8">
            <button className="btn ghost" onClick={loadAll}>刷新</button>
          </div>
        </div>
      </div>

      {/* 概览卡：名称编辑 + 操作 */}
      <div className="card">
        <div className="text-base font-semibold mb-3">基本信息</div>

        {!editing ? (
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="text-lg font-medium">
              名称：{binding.alias || <span className="small-muted">（未设置）</span>}
            </div>
            <div className="flex items-center gap-8">
              <button className="btn ghost" onClick={() => setEditing(true)}>编辑名称</button>
              <button className="btn ghost" onClick={onFreezeOrActivate}>
                {String(binding.status).toLowerCase() === 'active' ? '冻结' : '激活'}
              </button>
              <button className="btn ghost" onClick={onReauth}>重新授权</button>
              <button className="btn danger" onClick={onRemove}>移除授权</button>
            </div>
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-[1fr_auto_auto]">
            <input
              className="input"
              placeholder="输入名称（留空表示清除名称）"
              value={nameInput}
              onChange={(e) => setNameInput(e.target.value)}
            />
            <button
              className="btn"
              disabled={saving}
              onClick={async () => {
                try {
                  setSaving(true);
                  const res = await updateAlias(wid, auth_id, nameInput);
                  setBinding((b) => ({ ...(b || {}), alias: res?.alias ?? null }));
                  setEditing(false);
                } catch (e) {
                  alert(`保存失败：${e?.message || 'unknown error'}`);
                } finally {
                  setSaving(false);
                }
              }}
            >
              保存
            </button>
            <button className="btn ghost" onClick={() => { setEditing(false); setNameInput(binding.alias || ''); }}>
              取消
            </button>
          </div>
        )}
      </div>

      {/* 广告主列表卡（不再提供“同步广告主”按钮） */}
      <div className="card">
        <div className="text-base font-semibold mb-3">广告主</div>

        <div className="table-wrap">
          <table className="oauth-table">
            <thead>
              <tr>
                <th className="text-left px-2 py-2">Advertiser ID</th>
                <th className="text-left px-2 py-2">名称</th>
                <th className="text-left px-2 py-2">主标记</th>
                <th className="text-left px-2 py-2 col-actions" style={{ minWidth: 160 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {advertisers.length === 0 && (
                <tr>
                  <td className="px-2 py-6 small-muted" colSpan={4}>暂无数据</td>
                </tr>
              )}
              {advertisers.map((a) => {
                const isPrimary = String(a.advertiser_id) === String(primaryId);
                return (
                  <tr key={a.id}>
                    <td className="px-2 py-2">{a.advertiser_id}</td>
                    <td className="px-2 py-2">{a.name || '-'}</td>
                    <td className="px-2 py-2">
                      {isPrimary ? <span className="badge-role" style={{background:'#3b82f6'}}>PRIMARY</span> : <span className="small-muted">-</span>}
                    </td>
                    <td className="px-2 py-2">
                      <div className="table-actions">
                        <button
                          className="btn sm ghost"
                          disabled={isPrimary}
                          onClick={async () => {
                            try {
                              const res = await setPrimary(wid, auth_id, a.advertiser_id);
                              if ((res?.count ?? 0) > 0) {
                                const ads = await advertisersOf(wid, auth_id);
                                setAdvertisers(Array.isArray(ads) ? ads : []);
                              }
                            } catch (e) {
                              alert(`设置失败：${e?.message || 'unknown error'}`);
                            }
                          }}
                        >
                          设为主广告主
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

