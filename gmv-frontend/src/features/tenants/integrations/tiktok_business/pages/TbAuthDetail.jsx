// TikTok Business authorization detail page
import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
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
  const queryClient = useQueryClient();

  const [editing, setEditing] = useState(false);
  const [nameInput, setNameInput] = useState('');

  const bindingQuery = useQuery({
    queryKey: ['tb-binding', wid, auth_id],
    enabled: !!wid && !!auth_id,
    queryFn: async () => {
      const list = await listBindings(wid);
      const binding = list.find((x) => String(x.auth_id) === String(auth_id));
      if (!binding) {
        const err = new Error('未找到该授权记录');
        err.code = 'NOT_FOUND';
        throw err;
      }
      return binding;
    },
  });

  const advertisersQuery = useQuery({
    queryKey: ['tb-advertisers', wid, auth_id],
    enabled: bindingQuery.isSuccess,
    queryFn: async () => {
      const ads = await advertisersOf(wid, auth_id);
      return Array.isArray(ads) ? ads : [];
    },
  });

  useEffect(() => {
    if (bindingQuery.data) {
      setNameInput(bindingQuery.data.alias || '');
    }
  }, [bindingQuery.data]);

  const updateAliasMutation = useMutation({
    mutationFn: (alias) => updateAlias(wid, auth_id, alias),
    onSuccess: (res) => {
      queryClient.setQueryData(['tb-binding', wid, auth_id], (old) => ({
        ...(old || {}),
        alias: res?.alias ?? null,
      }));
      setEditing(false);
    },
  });

  const revokeMutation = useMutation({
    mutationFn: (remote) => revokeBinding(wid, auth_id, remote),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tb-binding', wid, auth_id] });
      queryClient.invalidateQueries({ queryKey: ['tb-bindings', wid] });
    },
  });

  const hardDeleteMutation = useMutation({
    mutationFn: () => hardDeleteBinding(wid, auth_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tb-bindings', wid] });
      nav(`/tenants/${encodeURIComponent(wid)}/tiktok-business`);
    },
  });

  const setPrimaryMutation = useMutation({
    mutationFn: (advertiserId) => setPrimary(wid, auth_id, advertiserId),
    onSuccess: () => {
      advertisersQuery.refetch();
    },
  });

  const binding = bindingQuery.data;
  const advertisers = advertisersQuery.data || [];
  const primaryId = useMemo(
    () => (advertisers.find((a) => a.primary_flag) || {}).advertiser_id,
    [advertisers],
  );

  const loading = bindingQuery.isLoading || bindingQuery.isFetching || (bindingQuery.isSuccess && (advertisersQuery.isLoading || advertisersQuery.isFetching));

  const error = bindingQuery.error;
  if (loading) return <div className="p-6">加载中…</div>;
  if (error) {
    return (
      <div className="p-6 space-y-3">
        <div className="text-red-500">错误：{String(error.message || error)}</div>
        <button className="btn ghost" onClick={() => nav(-1)}>返回</button>
      </div>
    );
  }

  function refreshAll() {
    return Promise.all([
      bindingQuery.refetch(),
      advertisersQuery.refetch(),
    ]);
  }

  // 操作：冻结/激活/重新授权/移除
  async function onFreezeOrActivate() {
    if (String(binding.status).toLowerCase() === 'active') {
      if (!confirm('确定要冻结（撤销长期令牌）吗？')) return;
      await revokeMutation.mutateAsync(true);
      await refreshAll();
    } else {
      const return_to = `${window.location.origin}/tenants/${encodeURIComponent(wid)}/tiktok-business`;
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
    const return_to = `${window.location.origin}/tenants/${encodeURIComponent(wid)}/tiktok-business`;
    const { auth_url } = await createAuthz(wid, {
      provider_app_id: binding.provider_app_id,
      alias: binding.alias || null,
      return_to,
    });
    window.open(auth_url, '_blank', 'noopener,noreferrer');
  }

  async function onRemove() {
    if (!confirm('确定要移除该授权记录吗？此操作会直接删除本地记录。')) return;
    await hardDeleteMutation.mutateAsync();
  }

  return (
    <div className="p-4 md:p-6 space-y-12">
      {/* 面包屑 + 标题 */}
      <div className="card">
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="small-muted">
              <Link to={`/tenants/${encodeURIComponent(wid)}/tiktok-business`} className="hover:underline">← 返回列表</Link>
            </div>
            <div className="text-xl font-semibold mt-1">TikTok Business 授权 · 详情</div>
            <div className="small-muted mt-1">
              状态：{cnStatus(binding.status)}　·　授权时间：{fmt(binding.created_at)}
            </div>
          </div>
          <div className="flex items-center gap-8">
            <Link
              className="btn ghost"
              to={`/tenants/${encodeURIComponent(wid)}/integrations/tiktok-business/accounts`}
            >
              查看数据
            </Link>
            <button className="btn ghost" onClick={refreshAll} disabled={bindingQuery.isRefetching || advertisersQuery.isRefetching}>
              {(bindingQuery.isRefetching || advertisersQuery.isRefetching) ? '刷新中…' : '刷新'}
            </button>
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
              <button className="btn ghost" onClick={onFreezeOrActivate} disabled={revokeMutation.isPending}>
                {String(binding.status).toLowerCase() === 'active' ? '冻结' : '激活'}
              </button>
              <button className="btn ghost" onClick={onReauth}>重新授权</button>
              <button className="btn danger" onClick={onRemove} disabled={hardDeleteMutation.isPending}>
                {hardDeleteMutation.isPending ? '处理中…' : '移除授权'}
              </button>
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
              disabled={updateAliasMutation.isPending}
              onClick={async () => {
                try {
                  await updateAliasMutation.mutateAsync(nameInput);
                } catch (e) {
                  alert(`保存失败：${e?.message || 'unknown error'}`);
                }
              }}
            >
              {updateAliasMutation.isPending ? '保存中…' : '保存'}
            </button>
            <button className="btn ghost" onClick={() => { setEditing(false); setNameInput(binding.alias || ''); }}>
              取消
            </button>
          </div>
        )}
      </div>

      {/* 广告主列表卡 */}
      <div className="card">
        <div className="text-base font-semibold mb-3">广告主</div>

        {advertisersQuery.error && (
          <div className="alert alert--error mb-3">{advertisersQuery.error.message || '获取广告主失败'}</div>
        )}

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
                  <tr key={a.id || a.advertiser_id}>
                    <td className="px-2 py-2">{a.advertiser_id}</td>
                    <td className="px-2 py-2">{a.name || '-'}</td>
                    <td className="px-2 py-2">
                      {isPrimary ? <span className="badge-role" style={{background:'#3b82f6'}}>PRIMARY</span> : <span className="small-muted">-</span>}
                    </td>
                    <td className="px-2 py-2">
                      <div className="table-actions">
                        <button
                          className="btn sm ghost"
                          disabled={isPrimary || setPrimaryMutation.isPending}
                          onClick={async () => {
                            try {
                              await setPrimaryMutation.mutateAsync(a.advertiser_id);
                            } catch (e) {
                              alert(`设置失败：${e?.message || 'unknown error'}`);
                            }
                          }}
                        >
                          {setPrimaryMutation.isPending && !isPrimary ? '设置中…' : '设为主广告主'}
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
