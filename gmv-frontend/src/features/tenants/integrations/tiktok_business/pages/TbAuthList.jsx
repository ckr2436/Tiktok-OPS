// src/features/tenants/integrations/tiktok_business/pages/TbAuthList.jsx
import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  getTenantMeta,
  listBindings,
  listProviderApps,
  createAuthz,
  hardDeleteBinding,
} from '../service.js';

/* 英文状态 -> 中文展示 */
function cnStatus(s) {
  const v = String(s || '').toLowerCase();
  if (v === 'active') return '已授权';
  if (v === 'revoked') return '已撤销';
  if (v === 'inactive') return '已冻结';
  if (v === 'expired') return '已过期';
  return '未知';
}

/* 时间格式 */
function fmt(dt) {
  try {
    if (!dt) return '-';
    const d = new Date(dt);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  } catch {
    return String(dt || '-');
  }
}

/** 统一列配置（确保表头与行严格一致） */
const COLUMNS = [
  { key: 'name',   title: '名称',     width: '44%', align: 'left'  },
  { key: 'status', title: '状态',     width: 120,   align: 'left'  },
  { key: 'time',   title: '授权时间', width: 220,   align: 'left'  },
  { key: 'ops',    title: '操作',     width: 260,   align: 'center'},
];

/** 操作按钮统一尺寸 */
const BTN = { width: 104, height: 32, gap: 12 };

export default function TbAuthList() {
  const { wid } = useParams();

  const [companyName, setCompanyName] = useState('');
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);

  const [providers, setProviders] = useState([]);
  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState('');
  const [newPid, setNewPid] = useState('');

  const title = useMemo(() => 'TikTok Business 授权', []);

  /* 读取公司名（不从 session 取） */
  useEffect(() => {
    getTenantMeta(wid)
      .then((m) => setCompanyName(m?.name || ''))
      .catch(() => setCompanyName(''));
  }, [wid]);

  /* 回调参数提示并刷新 */
  useEffect(() => {
    const qs = new URLSearchParams(window.location.search || '');
    if (qs.has('ok')) {
      const ok = qs.get('ok') === '1';
      const msg = ok ? '授权成功' : `授权失败：${qs.get('code') || ''} ${qs.get('msg') || ''}`;
      alert(msg.trim());
      const url = window.location.origin + window.location.pathname;
      window.history.replaceState({}, '', url);
      refresh();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function refresh() {
    setLoading(true);
    try {
      const list = await listBindings(wid);
      const safe = list.map((x) => ({
        auth_id: x.auth_id,
        provider_app_id: x.provider_app_id,
        name: x.alias || '-',             // alias -> 名称
        status: x.status,
        created_at: x.created_at,
      }));
      setRows(safe);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    listProviderApps(wid)
      .then((items) => {
        setProviders(items || []);
        if (items && items.length === 1) {
          setNewPid(String(items[0].id)); // 只有 1 个时自动选中且在 UI 隐藏选择器
        } else {
          setNewPid('');
        }
      })
      .catch(() => {
        setProviders([]);
        setNewPid('');
      });
  }, [wid]);

  /* 打开“新建授权”弹窗（可复用某行的 provider 与名称） */
  function openNewAuthDialog(prefill) {
    if (prefill) {
      setNewName(prefill.name && prefill.name !== '-' ? prefill.name : '');
      setNewPid(String(prefill.provider_app_id || ''));
    } else {
      setNewName('');
    }
    setShowNew(true);
  }

  async function handleCreateSubmit() {
    const pid = Number(newPid || (providers[0]?.id ?? 0));
    if (!pid) return;
    const return_to = `${window.location.origin}/tenants/${encodeURIComponent(wid)}/tiktok_business`;
    const { auth_url } = await createAuthz(wid, {
      provider_app_id: pid,
      return_to,
      alias: newName.trim() || null,
    });
    // 当前页跳转，回调后返回 return_to
    window.location.assign(auth_url);
  }

  /* 重新授权：弹出新建授权对话框，预填原 provider 与名称 */
  function handleReauth(row) {
    openNewAuthDialog(row);
  }

  /* 取消授权：直接硬删记录 */
  async function handleCancel(row) {
    if (!confirm('确定要取消授权吗？此操作会删除该授权记录。')) return;
    await hardDeleteBinding(wid, row.auth_id);
    await refresh();
  }

  const onlyOneProvider = providers.length === 1;

  return (
    <div className="p-4 md:p-6 space-y-12">
      {/* 顶部标题卡片 */}
      <div className="card">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-xl font-semibold">{title}</div>
            <div className="small-muted">
              {companyName ? `公司：${companyName}` : '公司'}
              <span> · 共 {rows.length} 个授权</span>
            </div>
          </div>
          <div className="flex items-center" style={{ columnGap: 14 }}>
            <button className="btn ghost" onClick={refresh}>刷新</button>
            <button className="btn" onClick={() => openNewAuthDialog()}>
              新建授权
            </button>
          </div>
        </div>
      </div>

      {/* 列表卡片 */}
      <div className="card">
        <div className="text-base font-semibold mb-3">授权列表</div>

        <div className="table-wrap">
          <table className="oauth-table" style={{ width: '100%', borderCollapse: 'separate', borderSpacing: 0 }}>
            <thead>
              <tr>
                {COLUMNS.map(col => (
                  <th
                    key={col.key}
                    className="px-2 py-2"
                    style={{
                      width: typeof col.width === 'number' ? `${col.width}px` : col.width,
                      textAlign: col.align,
                    }}
                    scope="col"
                  >
                    {col.title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td className="px-2 py-3" colSpan={COLUMNS.length}>加载中…</td>
                </tr>
              )}

              {!loading && rows.length === 0 && (
                <tr>
                  <td className="px-2 py-6 small-muted" colSpan={COLUMNS.length}>暂无授权</td>
                </tr>
              )}

              {!loading && rows.map((r) => (
                <tr key={r.auth_id}>
                  {/* 名称 */}
                  <td
                    className="px-2 py-2 truncate"
                    style={{ textAlign: COLUMNS[0].align }}
                    title={r.name || '-'}
                  >
                    {r.name || '-'}
                  </td>
                  {/* 状态 */}
                  <td className="px-2 py-2" style={{ textAlign: COLUMNS[1].align }}>
                    {cnStatus(r.status)}
                  </td>
                  {/* 时间 */}
                  <td className="px-2 py-2" style={{ textAlign: COLUMNS[2].align, fontVariantNumeric: 'tabular-nums' }}>
                    {fmt(r.created_at)}
                  </td>
                  {/* 操作 */}
                  <td className="px-2 py-2" style={{ textAlign: COLUMNS[3].align }}>
                    <div style={{ display: 'inline-flex', alignItems: 'center' }}>
                      <button
                        className="btn sm ghost"
                        style={{ width: BTN.width, height: BTN.height, marginRight: BTN.gap }}
                        onClick={() => handleReauth(r)}
                      >
                        重新授权
                      </button>
                      <button
                        className="btn sm danger"
                        style={{ width: BTN.width, height: BTN.height }}
                        onClick={() => handleCancel(r)}
                      >
                        取消授权
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* 新建授权：弹窗（Provider 只有一个时隐藏选择器） */}
      {showNew && (
        <div className="modal-backdrop" onClick={() => setShowNew(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal__header">
              <div className="modal__title">新建授权</div>
              <button className="modal__close" onClick={() => setShowNew(false)}>关闭</button>
            </div>
            <div className="modal__body">
              <div className="form">
                <div className="form-field">
                  <label className="form-field__label">名称</label>
                  <div className="form-field__control">
                    <input
                      className="input"
                      placeholder="给此次授权取个名称（可选）"
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                    />
                  </div>
                </div>

                <div className="form-field">
                  <label className="form-field__label">Provider App</label>
                  {onlyOneProvider ? (
                    <div className="input" style={{ display: 'flex', alignItems: 'center' }}>
                      <span className="truncate">{providers[0]?.name || '-'}</span>
                    </div>
                  ) : (
                    <div className="form-field__control">
                      <select
                        className="input"
                        value={newPid}
                        onChange={(e) => setNewPid(e.target.value)}
                      >
                        <option value="">请选择</option>
                        {providers.map((p) => (
                          <option key={p.id} value={String(p.id)}>
                            {p.name}（App ID: {p.client_id}）
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>
              </div>

              <div className="actions mt-4" style={{ display: 'flex', columnGap: BTN.gap }}>
                <button
                  className="btn"
                  style={{ width: BTN.width, height: BTN.height }}
                  disabled={(providers.length > 1 && !newPid) || providers.length === 0}
                  onClick={handleCreateSubmit}
                >
                  去授权
                </button>
                <button
                  className="btn ghost"
                  style={{ width: BTN.width, height: BTN.height }}
                  onClick={() => setShowNew(false)}
                >
                  取消
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

