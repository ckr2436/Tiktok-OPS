// TikTok Business authorization list page
import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
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
  const queryClient = useQueryClient();

  const [showNew, setShowNew] = useState(false);
  const [newName, setNewName] = useState('');
  const [newPid, setNewPid] = useState('');

  const title = useMemo(() => 'TikTok Business 授权', []);

  const metaQuery = useQuery({
    queryKey: ['tenant-meta', wid],
    queryFn: () => getTenantMeta(wid),
    enabled: !!wid,
  });

  const bindingsQuery = useQuery({
    queryKey: ['tb-bindings', wid],
    queryFn: async () => {
      const list = await listBindings(wid);
      if (!Array.isArray(list)) return [];
      return list.map((x) => ({
        auth_id: x.auth_id,
        provider_app_id: x.provider_app_id,
        name: x.alias || '-',
        status: x.status,
        created_at: x.created_at,
      }));
    },
    enabled: !!wid,
  });

  const providersQuery = useQuery({
    queryKey: ['tb-provider-apps', wid],
    queryFn: async () => {
      const items = await listProviderApps(wid);
      return Array.isArray(items) ? items : [];
    },
    enabled: !!wid,
  });

  useEffect(() => {
    const providers = providersQuery.data || [];
    if (providers.length === 1) {
      setNewPid((prev) => prev || String(providers[0].id ?? ''));
    } else if (!showNew) {
      setNewPid('');
    }
  }, [providersQuery.data, showNew]);

  /* 回调参数提示并刷新 */
  useEffect(() => {
    const qs = new URLSearchParams(window.location.search || '');
    if (qs.has('ok')) {
      const ok = qs.get('ok') === '1';
      const msg = ok ? '授权成功' : `授权失败：${qs.get('code') || ''} ${qs.get('msg') || ''}`;
      alert(msg.trim());
      const url = window.location.origin + window.location.pathname;
      window.history.replaceState({}, '', url);
      bindingsQuery.refetch();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const createAuthzMutation = useMutation({
    mutationFn: (payload) => createAuthz(wid, payload),
  });

  const hardDeleteMutation = useMutation({
    mutationFn: (authId) => hardDeleteBinding(wid, authId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tb-bindings', wid] });
    },
  });

  async function handleCreateSubmit() {
    const providers = providersQuery.data || [];
    const pid = Number(newPid || (providers[0]?.id ?? 0));
    if (!pid) return;
    const return_to = `${window.location.origin}/tenants/${encodeURIComponent(wid)}/tiktok-business`;
    const { auth_url } = await createAuthzMutation.mutateAsync({
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
    await hardDeleteMutation.mutateAsync(row.auth_id);
  }

  const rows = bindingsQuery.data || [];
  const loading = bindingsQuery.isLoading || bindingsQuery.isFetching;
  const error = bindingsQuery.error ? (bindingsQuery.error.message || '加载失败') : '';
  const providers = providersQuery.data || [];
  const onlyOneProvider = providers.length === 1;
  const companyName = metaQuery.data?.name || '';

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
            <Link
              className="btn ghost"
              to={`/tenants/${encodeURIComponent(wid)}/integrations/tiktok-business/accounts`}
            >
              查看数据
            </Link>
            <button className="btn ghost" onClick={() => bindingsQuery.refetch()} disabled={bindingsQuery.isRefetching}>
              {bindingsQuery.isRefetching ? '刷新中…' : '刷新'}
            </button>
            <button className="btn" onClick={() => openNewAuthDialog()} disabled={createAuthzMutation.isPending}>
              新建授权
            </button>
          </div>
        </div>
      </div>

      {/* 列表卡片 */}
      <div className="card">
        <div className="text-base font-semibold mb-3">授权列表</div>

        {error && (
          <div className="alert alert--error mb-3">{error}</div>
        )}

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
              {loading ? (
                <tr>
                  <td colSpan={COLUMNS.length} className="py-6 text-center small-muted">
                    加载中…
                  </td>
                </tr>
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={COLUMNS.length} className="py-6 text-center small-muted">
                    暂无授权记录
                  </td>
                </tr>
              ) : (
                rows.map(row => (
                  <tr key={row.auth_id} className="border-t border-gray-200">
                    <td className="px-2 py-3">
                      <div className="font-medium">{row.name}</div>
                      <div className="small-muted">
                        ID：{row.auth_id}
                      </div>
                    </td>
                    <td className="px-2 py-3">{cnStatus(row.status)}</td>
                    <td className="px-2 py-3">{fmt(row.created_at)}</td>
                    <td className="px-2 py-3">
                      <div className="flex flex-wrap items-center justify-center" style={{ gap: BTN.gap }}>
                        <button
                          className="btn ghost"
                          style={{ width: BTN.width, height: BTN.height }}
                          onClick={() => handleReauth(row)}
                        >
                          重新授权
                        </button>
                        <Link
                          className="btn ghost"
                          style={{ width: BTN.width, height: BTN.height }}
                          to={`/tenants/${encodeURIComponent(wid)}/tiktok-business/${encodeURIComponent(row.auth_id)}`}
                        >
                          查看详情
                        </Link>
                        <button
                          className="btn danger"
                          style={{ width: BTN.width, height: BTN.height }}
                          onClick={() => handleCancel(row)}
                          disabled={hardDeleteMutation.isPending}
                        >
                          {hardDeleteMutation.isPending ? '处理中…' : '取消授权'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 新建授权弹窗 */}
      {showNew && (
        <div className="dialog-backdrop">
          <div className="dialog">
            <div className="dialog-header">
              <div className="dialog-title">新建授权</div>
              <button className="btn ghost" onClick={() => setShowNew(false)}>
                关闭
              </button>
            </div>
            <div className="dialog-body space-y-4">
              {!onlyOneProvider && (
                <div>
                  <div className="small-muted mb-1">选择 Provider</div>
                  <select
                    className="input"
                    value={newPid}
                    onChange={(e) => setNewPid(e.target.value)}
                  >
                    <option value="">选择 provider-app</option>
                    {providers.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name || `App ${p.id}`}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div>
                <div className="small-muted mb-1">授权名称（可选）</div>
                <input
                  className="input"
                  placeholder="输入名称"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                />
              </div>
            </div>
            <div className="dialog-footer">
              <button className="btn ghost" onClick={() => setShowNew(false)}>
                取消
              </button>
              <button
                className="btn"
                onClick={handleCreateSubmit}
                disabled={createAuthzMutation.isPending || (!onlyOneProvider && !newPid)}
              >
                {createAuthzMutation.isPending ? '跳转中…' : '去授权'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
