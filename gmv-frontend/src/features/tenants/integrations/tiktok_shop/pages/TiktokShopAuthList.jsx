import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'

import {
  createTikTokShopAuthorization,
  deleteTikTokShopAccount,
  disconnectTikTokShopAccount,
  getTikTokShopReadiness,
  listTikTokShopAccounts,
  refreshTikTokShopAccount,
  syncTikTokShopAccount,
} from '../service.js'


function formatDate(value) {
  if (!value) return '-'
  const date = new Date(value.endsWith?.('Z') ? value : `${value}Z`)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}


function statusLabel(value) {
  const status = String(value || '').toLowerCase()
  if (status === 'active') return '已连接'
  if (status === 'revoked') return '已断开'
  if (status === 'expired') return '已过期'
  if (status === 'invalid') return '需重新授权'
  return '未知'
}


function errorText(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  return error?.message || '请求失败，请稍后重试。'
}


const oauthErrorMessages = Object.freeze({
  APP_CREDENTIALS_INVALID: 'TikTok Shop 应用凭据无效或已变更，请联系平台管理员更新配置后重新授权。',
  AUTH_CODE_INVALID_OR_EXPIRED: '授权码已过期或已被使用，请重新连接 TikTok Shop 并完成授权。',
  AUTH_SESSION_EXPIRED: '本次授权会话已过期，请重新发起连接。',
  AUTH_DENIED: '你已取消授权，TikTok Shop 未向本系统授予访问权限。',
  TIKTOK_SHOP_UNAVAILABLE: 'TikTok Shop 授权服务暂时不可用，请稍后重试。',
  TOKEN_EXCHANGE_FAILED: 'TikTok Shop 未能签发访问令牌，请重新授权；若仍失败，请联系平台管理员。',
  AUTHORIZATION_FAILED: 'TikTok Shop 授权未完成，请重新尝试。',
})


export function oauthCallbackErrorText(code, reason) {
  const normalizedCode = String(code || '').trim()
  const normalizedReason = String(reason || '').trim()
  const message = oauthErrorMessages[normalizedReason]
    || oauthErrorMessages[normalizedCode]
    || oauthErrorMessages.AUTHORIZATION_FAILED
  return normalizedCode ? `${message}（错误代码：${normalizedCode}）` : message
}


export default function TiktokShopAuthList() {
  const { wid } = useParams()
  const queryClient = useQueryClient()
  const [notice, setNotice] = useState(null)
  const [alias, setAlias] = useState('MYUPONA TikTok Shop')

  const readinessQuery = useQuery({
    queryKey: ['tiktok-shop-readiness', wid],
    queryFn: () => getTikTokShopReadiness(wid),
    enabled: Boolean(wid),
    staleTime: 5 * 60 * 1000,
  })
  const accountsQuery = useQuery({
    queryKey: ['tiktok-shop-accounts', wid],
    queryFn: () => listTikTokShopAccounts(wid),
    enabled: Boolean(wid),
    refetchInterval: 60 * 1000,
  })

  const refreshList = () => queryClient.invalidateQueries({ queryKey: ['tiktok-shop-accounts', wid] })

  const connectMutation = useMutation({
    mutationFn: () => createTikTokShopAuthorization(wid, {
      provider_app_id: readinessQuery.data?.provider_app_id || null,
      alias: alias.trim() || null,
      return_to: `/tenants/${encodeURIComponent(wid)}/tiktok-shop`,
    }),
    onSuccess: ({ auth_url: authUrl }) => window.location.assign(authUrl),
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })
  const syncMutation = useMutation({
    mutationFn: (accountId) => syncTikTokShopAccount(wid, accountId),
    onSuccess: async () => {
      await refreshList()
      setNotice({ type: 'success', text: '店铺与授权范围已同步。' })
    },
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })
  const tokenMutation = useMutation({
    mutationFn: (accountId) => refreshTikTokShopAccount(wid, accountId),
    onSuccess: async () => {
      await refreshList()
      setNotice({ type: 'success', text: '访问令牌已安全刷新。' })
    },
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })
  const disconnectMutation = useMutation({
    mutationFn: (accountId) => disconnectTikTokShopAccount(wid, accountId),
    onSuccess: async () => {
      await refreshList()
      setNotice({ type: 'success', text: '本系统已停止使用该授权。请同时在 TikTok Shop 后台移除应用访问权限。' })
    },
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })
  const deleteMutation = useMutation({
    mutationFn: (accountId) => deleteTikTokShopAccount(wid, accountId),
    onSuccess: async () => {
      await refreshList()
      setNotice({ type: 'success', text: '本地授权记录和令牌密文已删除。' })
    },
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const result = params.get('shop_oauth')
    if (!result) return
    if (result === 'success') {
      const count = Number(params.get('shop_count') || 0)
      setNotice({
        type: 'success',
        text: count > 0 ? `TikTok Shop 授权成功，已同步 ${count} 个店铺。` : 'TikTok Shop 授权成功，店铺资料正在等待同步。',
      })
      refreshList()
    } else {
      setNotice({
        type: 'error',
        text: oauthCallbackErrorText(params.get('code'), params.get('reason')),
      })
    }
    window.history.replaceState({}, '', window.location.pathname)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const accounts = accountsQuery.data || []
  const activeCount = accounts.filter((item) => item.status === 'active').length
  const shopCount = useMemo(
    () => accounts.reduce((total, item) => total + (item.shops || []).filter((shop) => shop.is_active).length, 0),
    [accounts],
  )
  const busy = syncMutation.isPending || tokenMutation.isPending || disconnectMutation.isPending || deleteMutation.isPending

  return (
    <div style={{ display: 'grid', gap: 18 }}>
      <header style={{ display: 'flex', justifyContent: 'space-between', gap: 20, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24 }}>TikTok Shop 授权</h1>
          <p className="small-muted" style={{ margin: '6px 0 0' }}>
            管理卖家授权、令牌有效期和已授权店铺。TikTok Shop 与 TikTok Business 使用独立凭证。
          </p>
        </div>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <Link className="btn ghost" to={`/tenants/${wid}/tiktok-shop/videos`}>短视频分析</Link>
          <button
            className="btn"
            onClick={() => connectMutation.mutate()}
            disabled={!readinessQuery.data?.configured || connectMutation.isPending}
          >
            {connectMutation.isPending ? '正在跳转...' : '连接 TikTok Shop'}
          </button>
        </div>
      </header>

      {!readinessQuery.isLoading && !readinessQuery.data?.configured && (
        <div className="alert alert--error">
          平台尚未配置 TikTok Shop App Key、Service ID 和 App Secret，请由平台 Owner 完成应用配置。
        </div>
      )}
      {notice && <div className={`alert alert--${notice.type}`}>{notice.text}</div>}

      <section className="card" aria-label="授权概览">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
          <Summary label="有效授权" value={activeCount} />
          <Summary label="已授权店铺" value={shopCount} />
          <Summary label="目标区域" value="美国" />
          <Summary label="回调状态" value={readinessQuery.data?.configured ? '已配置' : '待配置'} />
        </div>
      </section>

      <section className="card">
        <div style={{ display: 'flex', alignItems: 'end', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap', marginBottom: 14 }}>
          <div>
            <h2 style={{ margin: 0, fontSize: 18 }}>卖家授权</h2>
            <p className="small-muted" style={{ margin: '4px 0 0' }}>访问令牌仅以加密密文保存，不会在页面或日志中回显。</p>
          </div>
          <div style={{ display: 'flex', gap: 10, alignItems: 'end', flexWrap: 'wrap' }}>
            <label style={{ display: 'grid', gap: 5 }}>
              <span className="small-muted">授权名称</span>
              <input className="input" value={alias} onChange={(event) => setAlias(event.target.value)} maxLength={128} />
            </label>
            <button className="btn ghost" onClick={() => accountsQuery.refetch()} disabled={accountsQuery.isFetching}>
              {accountsQuery.isFetching ? '刷新中...' : '刷新列表'}
            </button>
          </div>
        </div>

        <div className="table-wrap">
          <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 980 }}>
            <thead style={{ background: 'var(--panel-2)' }}>
              <tr>
                <Th>授权主体</Th>
                <Th>状态</Th>
                <Th>已授权店铺</Th>
                <Th>访问令牌到期</Th>
                <Th>最近同步</Th>
                <Th>操作</Th>
              </tr>
            </thead>
            <tbody>
              {accountsQuery.isLoading ? (
                <tr><td colSpan={6} style={{ padding: 24 }}>正在加载授权...</td></tr>
              ) : accounts.length === 0 ? (
                <tr><td colSpan={6} style={{ padding: 24, color: 'var(--muted)' }}>暂无卖家授权。</td></tr>
              ) : accounts.map((account) => (
                <tr key={account.id} style={{ borderTop: '1px solid var(--border)' }}>
                  <Td>
                    <div style={{ fontWeight: 700 }}>{account.alias || account.seller_name || 'TikTok Shop 卖家'}</div>
                    <div className="small-muted">{account.open_id_masked}</div>
                    {account.last_error_message && <div style={{ color: '#b42318', marginTop: 4 }}>{account.last_error_message}</div>}
                  </Td>
                  <Td><Status value={account.status} /></Td>
                  <Td>
                    {(account.shops || []).length === 0 ? (
                      <span className="small-muted">尚未同步</span>
                    ) : (account.shops || []).map((shop) => (
                      <div key={shop.id} style={{ marginBottom: 4 }}>
                        <strong>{shop.shop_name || shop.shop_id}</strong>
                        <span className="small-muted"> {shop.region || ''}</span>
                      </div>
                    ))}
                  </Td>
                  <Td>{formatDate(account.expires_at)}</Td>
                  <Td>{formatDate(account.last_synced_at)}</Td>
                  <Td>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                      <button className="btn sm ghost" onClick={() => syncMutation.mutate(account.id)} disabled={busy || account.status !== 'active'}>同步店铺</button>
                      <button className="btn sm ghost" onClick={() => tokenMutation.mutate(account.id)} disabled={busy || account.status !== 'active'}>刷新令牌</button>
                      {account.status === 'active' ? (
                        <button
                          className="btn sm danger"
                          onClick={() => window.confirm('确定停止本系统使用该 TikTok Shop 授权吗？') && disconnectMutation.mutate(account.id)}
                          disabled={busy}
                        >
                          断开连接
                        </button>
                      ) : (
                        <button
                          className="btn sm danger"
                          onClick={() => window.confirm('确定永久删除本地授权记录和令牌密文吗？') && deleteMutation.mutate(account.id)}
                          disabled={busy}
                        >
                          删除记录
                        </button>
                      )}
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}


function Summary({ label, value }) {
  return (
    <div style={{ borderLeft: '3px solid #2563eb', padding: '4px 0 4px 12px' }}>
      <div className="small-muted">{label}</div>
      <div style={{ fontSize: 21, fontWeight: 750, marginTop: 3 }}>{value}</div>
    </div>
  )
}


function Status({ value }) {
  const active = value === 'active'
  return (
    <span style={{ display: 'inline-flex', padding: '4px 9px', borderRadius: 999, border: `1px solid ${active ? '#86efac' : '#cbd5e1'}`, background: active ? '#f0fdf4' : '#f8fafc', color: active ? '#166534' : '#475569' }}>
      {statusLabel(value)}
    </span>
  )
}


function Th({ children }) {
  return <th style={{ textAlign: 'left', padding: '10px 12px', fontWeight: 700 }}>{children}</th>
}


function Td({ children }) {
  return <td style={{ padding: '12px', verticalAlign: 'top' }}>{children}</td>
}
