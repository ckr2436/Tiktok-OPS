// TikTok Business authorization list page
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import {
  getTenantMeta,
  listBindings,
  listProviderApps,
  createAuthz,
  hardDeleteBinding,
} from '../service.js'

/* 英文状态 -> 中文展示 */
function cnStatus(s) {
  const v = String(s || '').toLowerCase()
  if (v === 'active') return '已授权'
  if (v === 'revoked') return '已撤销'
  if (v === 'inactive') return '已冻结'
  if (v === 'expired') return '已过期'
  return '未知'
}

/* 时间格式 */
function fmt(dt) {
  try {
    if (!dt) return '-'
    const d = new Date(dt)
    const p = (n) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  } catch {
    return String(dt || '-')
  }
}

/** 统一列配置 */
const COLUMNS = [
  { key: 'name', title: '名称', width: '44%', align: 'left' },
  { key: 'status', title: '状态', width: 120, align: 'left' },
  { key: 'time', title: '授权时间', width: 220, align: 'left' },
  { key: 'ops', title: '操作', width: 260, align: 'center' },
]

const BTN = { width: 104, height: 32, gap: 12 }

export default function TbAuthList() {
  const { wid } = useParams()
  const queryClient = useQueryClient()

  const [showNew, setShowNew] = useState(false)
  const [newName, setNewName] = useState('')
  const [newPid, setNewPid] = useState('')

  const title = useMemo(() => 'TikTok Business 授权', [])

  const metaQuery = useQuery({
    queryKey: ['tenant-meta', wid],
    queryFn: () => getTenantMeta(wid),
    enabled: Boolean(wid),
  })

  const bindingsQuery = useQuery({
    queryKey: ['tb-bindings', wid],
    queryFn: () => listBindings(wid),
    enabled: Boolean(wid),
  })

  const providersQuery = useQuery({
    queryKey: ['tb-provider-apps', wid],
    queryFn: () => listProviderApps(wid),
    enabled: Boolean(wid),
  })

  useEffect(() => {
    const providers = providersQuery.data || []
    if (providers.length === 1) {
      setNewPid(String(providers[0]?.id || ''))
    } else {
      setNewPid('')
    }
  }, [providersQuery.data])

  useEffect(() => {
    const qs = new URLSearchParams(window.location.search || '')
    if (!qs.has('ok')) return
    const ok = qs.get('ok') === '1'
    const msg = ok ? '授权成功' : `授权失败：${qs.get('code') || ''} ${qs.get('msg') || ''}`
    alert(msg.trim())
    const url = window.location.origin + window.location.pathname
    window.history.replaceState({}, '', url)
    bindingsQuery.refetch()
  }, [])

  const createMutation = useMutation({
    mutationFn: (payload) => createAuthz(wid, payload),
  })

  const deleteMutation = useMutation({
    mutationFn: (authId) => hardDeleteBinding(wid, authId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tb-bindings', wid] })
    },
  })

  const rows = Array.isArray(bindingsQuery.data) ? bindingsQuery.data.map((x) => ({
    auth_id: x.auth_id,
    provider_app_id: x.provider_app_id,
    name: x.alias || '-',
    status: x.status,
    created_at: x.created_at,
  })) : []
  const loading = bindingsQuery.isLoading || bindingsQuery.isFetching
  const companyName = metaQuery.data?.name || ''
  const providers = Array.isArray(providersQuery.data) ? providersQuery.data : []
  const onlyOneProvider = providers.length === 1

  function openNewAuthDialog(prefill) {
    if (prefill) {
      setNewName(prefill.name && prefill.name !== '-' ? prefill.name : '')
      setNewPid(String(prefill.provider_app_id || ''))
    } else {
      setNewName('')
    }
    setShowNew(true)
  }

  async function handleCreateSubmit() {
    const pid = Number(newPid || (providers[0]?.id ?? 0))
    if (!pid) return
    const return_to = `${window.location.origin}/tenants/${encodeURIComponent(wid)}/tiktok-business`
    try {
      const { auth_url } = await createMutation.mutateAsync({
        provider_app_id: pid,
        alias: newName.trim() || null,
        return_to,
      })
      window.location.assign(auth_url)
    } catch (err) {
      alert(err?.message || '发起授权失败')
    }
  }

  function handleReauth(row) {
    openNewAuthDialog(row)
  }

  async function handleCancel(row) {
    if (!confirm('确定要取消授权吗？此操作会删除该授权记录。')) return
    try {
      await deleteMutation.mutateAsync(row.auth_id)
    } catch (err) {
      alert(err?.message || '取消授权失败')
    }
  }

  return (
    <div className="p-4 md:p-6 space-y-12">
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
            <button className="btn ghost" onClick={() => bindingsQuery.refetch()}>刷新</button>
            <button className="btn" onClick={() => openNewAuthDialog()}>
              新建授权
            </button>
          </div>
        </div>
      </div>

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
                  <td
                    className="px-2 py-2 truncate"
                    style={{ textAlign: COLUMNS[0].align }}
                    title={r.name || '-'}
                  >
                    {r.name || '-'}
                  </td>
                  <td className="px-2 py-2" style={{ textAlign: COLUMNS[1].align }}>
                    {cnStatus(r.status)}
                  </td>
                  <td className="px-2 py-2" style={{ textAlign: COLUMNS[2].align, fontVariantNumeric: 'tabular-nums' }}>
                    {fmt(r.created_at)}
                  </td>
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
                        disabled={deleteMutation.isPending && deleteMutation.variables === r.auth_id}
                      >
                        {deleteMutation.isPending && deleteMutation.variables === r.auth_id ? '取消中…' : '取消授权'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

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
                  disabled={(providers.length > 1 && !newPid) || providers.length === 0 || createMutation.isPending}
                  onClick={handleCreateSubmit}
                >
                  {createMutation.isPending ? '跳转中…' : '去授权'}
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
  )
}
