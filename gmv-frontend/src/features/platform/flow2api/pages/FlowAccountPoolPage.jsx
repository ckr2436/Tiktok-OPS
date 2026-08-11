import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Loading from '../../../../components/ui/Loading.jsx'
import Modal from '../../../../components/ui/Modal.jsx'
import flowAccountApi from '../service.js'
import './FlowAccountPoolPage.css'

const EMPTY_IMPORT = {
  device_id: '',
  remark: '',
  image_enabled: false,
  video_enabled: true,
  image_concurrency: 1,
  video_concurrency: 1,
  proxy_id: '',
}

const EMPTY_PROXY = { name: '', proxy_url: '', is_active: true }

function time(value) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function tierLabel(value) {
  const normalized = String(value || '').toUpperCase()
  if (normalized.includes('TIER_TWO')) return '高级会员'
  if (normalized.includes('TIER_ONE')) return '付费会员'
  if (normalized.includes('NOT_PAID')) return '免费账号'
  return value || '待识别'
}

export function currentFlowFailureCode(account) {
  const code = String(account?.last_failure_code || '').trim()
  if (!code) return ''
  const status = String(account?.last_keepalive_status || '').trim().toLowerCase()
  const failures = Number(account?.keepalive_failure_count)
  if (['success', 'ok', 'alive', 'healthy'].includes(status)) return ''
  if (Number.isFinite(failures) && failures === 0 && account?.last_keepalive_success_at) return ''
  return code
}

export function authorizationState(account, now = Date.now()) {
  const upstream = String(account?.auth_state || '').trim().toLowerCase()
  const banReason = String(account?.ban_reason || '').trim().toUpperCase()
  if (upstream === 'blocked' || ['GRANT_EXPIRED', 'ST_REVOKED'].includes(banReason)) return 'blocked'
  if (upstream === 'missing' || !account?.has_at) return 'missing'
  const expiresAt = Date.parse(account?.at_expires || '')
  if (Number.isFinite(expiresAt)) {
    if (expiresAt <= now) return 'expired'
    if (expiresAt - now <= 20 * 60 * 1000) return 'refresh_due'
  }
  return 'ready'
}

export function isRoutable(account, now = Date.now()) {
  if (!account?.is_active || account?.ban_reason) return false
  if (account?.routable === false) return false
  return authorizationState(account, now) === 'ready' || authorizationState(account, now) === 'refresh_due'
}

export function authorizationLabel(account, now = Date.now()) {
  return {
    blocked: '授权阻断，需重新授权',
    missing: '缺少授权凭据',
    expired: '授权已过期，等待续期',
    refresh_due: '授权即将到期，保活续期中',
    ready: '授权有效',
  }[authorizationState(account, now)]
}

export function stateClass(account) {
  if (!account.is_active) return 'is-offline'
  if (!isRoutable(account) || currentFlowFailureCode(account)) return 'is-warning'
  return 'is-healthy'
}

export function profileCapacityText(device) {
  const used = Math.max(0, Number(device?.profile_used_count) || 0)
  const capacity = Math.max(used, Number(device?.profile_capacity) || 0)
  const usage = device?.profile_usage || {}
  return `Profile ${used}/${capacity} · Flow ${Number(usage.flow) || 0} · 豆包 ${Number(usage.doubao) || 0} · 即梦 ${Number(usage.jimeng) || 0} · 下载 ${Number(usage.yt_dlp) || 0} · 内容 ${Number(usage.content) || 0}`
}

export function isImportSessionComplete(session) {
  return String(session?.state || '').toLowerCase() === 'ready'
}

function errorText(error) {
  return error?.response?.data?.detail || error?.message || '操作失败，请稍后重试。'
}

function flowBridgeDeviceId() {
  const key = 'platform-flow2api-bridge-device'
  let value = window.localStorage.getItem(key) || ''
  if (!value) {
    value = `flowdev_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`
    window.localStorage.setItem(key, value)
  }
  return value
}

function downloadBlob(blob, filename) {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(url)
}

export default function FlowAccountPoolPage() {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState(null)
  const [importOpen, setImportOpen] = useState(false)
  const [editAccount, setEditAccount] = useState(null)
  const [importForm, setImportForm] = useState(EMPTY_IMPORT)
  const [editForm, setEditForm] = useState(null)
  const [activeSessionId, setActiveSessionId] = useState(null)
  const [reauthSessionId, setReauthSessionId] = useState(null)
  const [reauthTarget, setReauthTarget] = useState(null)
  const [reauthOpen, setReauthOpen] = useState(false)
  const [closingReauth, setClosingReauth] = useState(false)
  const [closingImport, setClosingImport] = useState(false)
  const [proxyOpen, setProxyOpen] = useState(false)
  const [editingProxy, setEditingProxy] = useState(null)
  const [proxyForm, setProxyForm] = useState(EMPTY_PROXY)

  const query = useQuery({
    queryKey: ['platform', 'flow2api', 'account-pool'],
    queryFn: flowAccountApi.overview,
    staleTime: 15_000,
    refetchInterval: (current) => {
      const sessions = current.state.data?.browser_sessions || []
      return sessions.some((item) => ['awaiting_login', 'capture_pending', 'keepalive_pending'].includes(item.state)) ? 3_000 : 60_000
    },
  })
  const overview = query.data || {}
  const tokens = overview.tokens || []
  const stats = overview.stats || {}
  const devices = overview.devices || []
  const bridgeHosts = overview.bridge_hosts || []
  const sessions = overview.browser_sessions || []
  const activeSession = sessions.find((item) => item.session_id === activeSessionId)
  const reauthSession = sessions.find((item) => item.session_id === reauthSessionId)
  const onlineDevices = devices.filter((item) => item.online && item.bound)
  const profileReadyDevices = onlineDevices.filter((item) => Number(item.profile_available_count) > 0)
  const proxies = overview.proxies || []
  const activeProxies = proxies.filter((item) => item.is_active)
  const proxyMap = useMemo(() => Object.fromEntries(proxies.map((item) => [Number(item.id), item])), [proxies])
  const activeCount = useMemo(() => tokens.filter((item) => isRoutable(item)).length, [tokens])
  const usableCredits = useMemo(
    () => tokens.reduce((sum, item) => sum + (isRoutable(item) ? Math.max(0, Number(item.credits) || 0) : 0), 0),
    [tokens],
  )

  const action = useMutation({
    mutationFn: async ({ fn }) => fn(),
    onSuccess: async (_result, variables) => {
      setMessage({ type: 'success', text: variables.success })
      await queryClient.invalidateQueries({ queryKey: ['platform', 'flow2api', 'account-pool'] })
    },
    onError: (error) => setMessage({ type: 'error', text: errorText(error) }),
  })

  const submitImport = (event) => {
    event.preventDefault()
    setMessage(null)
    action.mutate({
      fn: () => flowAccountApi.startBrowserSession({
        ...importForm,
        proxy_id: Number(importForm.proxy_id),
        image_concurrency: Number(importForm.image_concurrency),
        video_concurrency: Number(importForm.video_concurrency),
      }),
      success: '已在所选设备打开固定 Chrome Profile，请完成 Google 登录',
    }, {
      onSuccess: (result) => {
        setActiveSessionId(result?.session_id || null)
      },
    })
  }

  const openImport = () => {
    const preferred = profileReadyDevices[0]?.device_id || ''
    setActiveSessionId(null)
    setImportForm({ ...EMPTY_IMPORT, device_id: preferred, proxy_id: activeProxies[0]?.id || '' })
    setImportOpen(true)
  }

  const closeImport = async () => {
    if (action.isPending || closingImport) return
    const terminal = ['ready', 'failed', 'cancelled'].includes(activeSession?.state)
    if (activeSessionId && !terminal) {
      setClosingImport(true)
      try {
        await flowAccountApi.cancelBrowserSession(activeSessionId)
        await queryClient.invalidateQueries({ queryKey: ['platform', 'flow2api', 'account-pool'] })
      } catch (error) {
        setMessage({ type: 'error', text: `关闭账号添加失败：${errorText(error)}` })
        setClosingImport(false)
        return
      }
      setClosingImport(false)
    }
    setImportOpen(false)
    setActiveSessionId(null)
  }

  const reauthAccount = (account) => {
    const binding = sessions.find((item) => Number(item.token_id) === Number(account.id) && item.device_id)
    if (!binding) {
      setMessage({ type: 'error', text: '该账号没有固定 Windows Profile，不能切换到其他 Slot 重新登录。' })
      return
    }
    setMessage(null)
    setReauthTarget(account)
    setReauthSessionId(null)
    setReauthOpen(true)
    action.mutate({
      fn: () => flowAccountApi.browserReauth(account.id, {
        device_id: binding.device_id,
        remark: account.remark || '',
        image_enabled: !!account.image_enabled,
        video_enabled: !!account.video_enabled,
        image_concurrency: Number(account.image_concurrency ?? 1),
        video_concurrency: Number(account.video_concurrency ?? 1),
        proxy_id: account.proxy_id || activeProxies[0]?.id || '',
      }),
      success: '已打开该账号原来的固定 Chrome Profile，请重新登录',
    }, {
      onSuccess: (result) => {
        setReauthSessionId(result?.session_id || null)
      },
    })
  }

  const closeReauth = async () => {
    if (action.isPending || closingReauth) return
    const terminal = ['ready', 'failed', 'cancelled'].includes(reauthSession?.state)
    if (reauthSessionId && !terminal) {
      setClosingReauth(true)
      try {
        await flowAccountApi.cancelBrowserSession(reauthSessionId)
        await queryClient.invalidateQueries({ queryKey: ['platform', 'flow2api', 'account-pool'] })
      } catch (error) {
        setMessage({ type: 'error', text: `停止重新授权失败：${errorText(error)}` })
        setClosingReauth(false)
        return
      }
      setClosingReauth(false)
    }
    setReauthOpen(false)
    setReauthSessionId(null)
    setReauthTarget(null)
  }

  const openEdit = (account) => {
    setEditAccount(account)
    setEditForm({
      remark: account.remark || '',
      image_enabled: !!account.image_enabled,
      video_enabled: !!account.video_enabled,
      image_concurrency: account.image_concurrency ?? 1,
      video_concurrency: account.video_concurrency ?? 1,
      proxy_id: account.proxy_id || activeProxies[0]?.id || '',
    })
  }

  const submitEdit = (event) => {
    event.preventDefault()
    action.mutate({
      fn: () => flowAccountApi.updateAccount(editAccount.id, {
        ...editForm,
        proxy_id: Number(editForm.proxy_id),
        image_concurrency: Number(editForm.image_concurrency),
        video_concurrency: Number(editForm.video_concurrency),
      }),
      success: `${editAccount.email || `账号 ${editAccount.id}`} 配置已更新`,
    }, { onSuccess: () => { setEditAccount(null); setEditForm(null) } })
  }

  const toggleAccount = (account) => {
    if (account.is_active && !window.confirm(`确定暂停 ${account.email || `账号 ${account.id}`} 的业务流量吗？`)) return
    action.mutate({
      fn: () => account.is_active ? flowAccountApi.disable(account.id) : flowAccountApi.enable(account.id),
      success: `账号已${account.is_active ? '暂停' : '启用'}`,
    })
  }

  const removeAccount = (account) => {
    if (!window.confirm(`确定从号池删除 ${account.email || `账号 ${account.id}`}？此操作不可撤销。`)) return
    action.mutate({
      fn: () => flowAccountApi.remove(account.id),
      success: '账号已从号池删除',
    })
  }

  const openProxyCreate = () => {
    setEditingProxy(null)
    setProxyForm(EMPTY_PROXY)
    setProxyOpen(true)
  }

  const openProxyEdit = (proxy) => {
    setEditingProxy(proxy)
    setProxyForm({ name: proxy.name, proxy_url: '', is_active: !!proxy.is_active })
    setProxyOpen(true)
  }

  const submitProxy = (event) => {
    event.preventDefault()
    const payload = { name: proxyForm.name.trim(), is_active: !!proxyForm.is_active }
    if (proxyForm.proxy_url.trim()) payload.proxy_url = proxyForm.proxy_url.trim()
    action.mutate({
      fn: () => editingProxy
        ? flowAccountApi.updateProxy(editingProxy.id, payload)
        : flowAccountApi.createProxy(payload),
      success: editingProxy ? '代理配置已更新' : '代理已加入平台代理池',
    }, { onSuccess: () => { setProxyOpen(false); setEditingProxy(null); setProxyForm(EMPTY_PROXY) } })
  }

  const toggleProxy = (proxy) => {
    action.mutate({
      fn: () => flowAccountApi.updateProxy(proxy.id, { is_active: !proxy.is_active }),
      success: `代理已${proxy.is_active ? '停用' : '启用'}`,
    })
  }

  const removeProxy = (proxy) => {
    if (!window.confirm(`确定删除代理“${proxy.name}”吗？`)) return
    action.mutate({
      fn: () => flowAccountApi.removeProxy(proxy.id),
      success: '代理已删除',
    })
  }

  const installBridge = async (device = null) => {
    setMessage(null)
    try {
      const blob = await flowAccountApi.downloadBridgeAgent({
        deviceId: device?.device_id || flowBridgeDeviceId(),
        deviceName: device?.device_name || navigator.platform || 'Windows device',
      })
      downloadBlob(blob, 'MYUPONA-HermesBridge.exe')
      setMessage({ type: 'success', text: '浏览器桥已下载。运行一次后，本页面会自动显示在线设备。' })
    } catch (error) {
      setMessage({ type: 'error', text: errorText(error) })
    }
  }

  const assignExistingHost = (device, host) => {
    action.mutate({
      fn: () => flowAccountApi.assignBridgeHost({
        target_device_id: device.device_id,
        source_workspace_id: Number(host.workspace_id),
        source_user_id: Number(host.user_id),
        source_device_id: host.device_id,
      }),
      success: `已授权 ${device.device_name || device.device_id} 加入 ${host.device_name || host.device_id}；Windows 客户端会自动加载绑定`,
    })
  }

  return (
    <div className="flow-pool">
      <header className="page-header flow-pool__header">
        <div>
          <h1 className="page-title">Flow 视频账号池</h1>
          <div className="small-muted">平台管理员专属 · 统一维护 Google Flow 账号、额度、业务状态和并发。</div>
        </div>
        <div className="flow-pool__actions">
          <button type="button" className="btn" disabled={query.isFetching} onClick={() => query.refetch()}>刷新状态</button>
          <button type="button" className="btn" onClick={openProxyCreate}>添加代理</button>
          <button type="button" className="btn btn--primary" disabled={!profileReadyDevices.length || !activeProxies.length} onClick={openImport}>添加账号</button>
        </div>
      </header>

      {message && <div className={`alert alert--${message.type === 'error' ? 'error' : 'success'} flow-pool__notice`}>{message.text}</div>}
      {query.error && <div className="alert alert--error flow-pool__notice">{errorText(query.error)}</div>}

      <section className="flow-pool__summary">
        <article><span>号池状态</span><strong className={overview.service?.status === 'healthy' ? 'good' : 'bad'}>{overview.service?.status === 'healthy' ? '运行正常' : '不可用'}</strong><small>Flow2API 本机通道</small></article>
        <article><span>可调度账号</span><strong>{activeCount} / {tokens.length}</strong><small>启用且真实授权通过才进入任务路由</small></article>
        <article><span>可调度额度</span><strong>{usableCredits}</strong><small>只统计当前可路由账号</small></article>
        <article><span>视频生成</span><strong>{stats.today_videos || 0} / {stats.total_videos || 0}</strong><small>今日 / 累计</small></article>
        <article><span>参考图能力</span><strong>{overview.capabilities?.reference_image_limit || 0} 张</strong><small>当前本地 Flow 通道</small></article>
      </section>

      <div className="flow-pool__safety">
        <strong>账号接入说明</strong>
        <span>添加账号时会先打开不带自动化参数的正常 Chrome。请只在 Flow 网页内点击 Google 登录，不要点击 Chrome 右上角开启浏览器同步；确认 Flow 可访问后关闭整个窗口。系统随后会用同一固定 Profile 自动采集必要登录态与真实浏览器指纹，前端不会显示或保存 Cookie。</span>
        {!onlineDevices.length && <em>当前没有在线且已绑定的 Hermes Bridge 设备。<button type="button" className="flow-pool__inline-action" onClick={() => installBridge()}>下载 Windows 浏览器桥</button>，运行一次后再添加账号。</em>}
        {!onlineDevices.length && !!devices.length && !!bridgeHosts.length && (
          <em>
            检测到其他已认证的 Windows 宿主，可复用同一台电脑而不覆盖租户绑定：
            {devices.filter((device) => !device.online).slice(0, 1).map((device) => bridgeHosts.slice(0, 3).map((host) => (
              <button
                type="button"
                className="flow-pool__inline-action"
                key={`${device.device_id}-${host.workspace_id}-${host.user_id}-${host.device_id}`}
                disabled={action.isPending}
                onClick={() => assignExistingHost(device, host)}
              >
                绑定到 {host.device_name || host.device_id}
              </button>
            )))}
          </em>
        )}
        {!!onlineDevices.length && <div className="flow-pool__device-capacity">{onlineDevices.map((device) => <span key={device.device_id} className={Number(device.profile_available_count) > 0 ? '' : 'is-full'}><strong>{device.device_name}</strong>{profileCapacityText(device)}{Number(device.profile_reclaimable_count) > 0 ? ` · 可安全回收 ${device.profile_reclaimable_count}` : ''}</span>)}</div>}
        {devices.filter((device) => device.agent_update_required).map((device) => (
          <em key={`update-${device.device_id}`}>
            {device.device_name} 客户端 {device.agent_version || '未知'}，服务器最新版 {device.server_agent_version || '未知'}。
            {device.online && device.agent_update_state !== 'failed' ? ' 正在自动更新；' : ' 自动更新不可用；'}
            <button type="button" className="flow-pool__inline-action" onClick={() => installBridge(device)}>下载最新版 EXE</button>
          </em>
        ))}
        {!!onlineDevices.length && !profileReadyDevices.length && <em>在线设备的持久 Profile 已满。系统只会自动回收确认未绑定账号的终止开户残留，不会复用现有账号 Profile。</em>}
        {!activeProxies.length && <em>当前没有启用的代理。请先点击“添加代理”，账号登录、保活和生成任务才会使用固定出口。</em>}
      </div>

      {!query.isLoading && <section className="card flow-pool__proxy-card">
        <div className="card__header"><span>代理池</span><span className="small-muted">一个代理可绑定多个账号；地址与凭据在服务端加密保存。</span></div>
        <div className="table-wrapper"><table className="table flow-pool__proxy-table"><thead><tr><th>名称</th><th>代理端点</th><th>状态</th><th>已绑定账号</th><th>操作</th></tr></thead><tbody>
          {proxies.map((proxy) => <tr key={proxy.id}><td><strong>{proxy.name}</strong></td><td><code>{proxy.display_url}</code></td><td>{proxy.is_active ? <span className="good">已启用</span> : <span>已停用</span>}</td><td>{proxy.in_use_count || 0}</td><td><div className="flow-pool__row-actions"><button type="button" className="btn btn--sm" onClick={() => openProxyEdit(proxy)}>编辑</button><button type="button" className="btn btn--sm" onClick={() => toggleProxy(proxy)}>{proxy.is_active ? '停用' : '启用'}</button><button type="button" className="btn btn--sm btn--danger" disabled={!!proxy.in_use_count} onClick={() => removeProxy(proxy)}>删除</button></div></td></tr>)}
          {!proxies.length && <tr><td colSpan="5" className="empty">尚未配置代理。添加后才能接入 Flow 账号。</td></tr>}
        </tbody></table></div>
      </section>}

      {query.isLoading ? <Loading text="正在读取 Flow 号池…" /> : (
        <section className="card flow-pool__table-card">
          <div className="card__header">账号列表</div>
          <div className="table-wrapper">
            <table className="table flow-pool__table">
              <thead><tr><th>账号</th><th>会员与额度</th><th>业务能力</th><th>登录状态</th><th>使用情况</th><th>操作</th></tr></thead>
              <tbody>
            {tokens.map((account) => {
              const currentFailure = currentFlowFailureCode(account)
              const routable = isRoutable(account)
              return <tr key={account.id}>
                  <td><div className="flow-account"><span className={`flow-account__dot ${stateClass(account)}`} /><div><strong>{account.email || `账号 ${account.id}`}</strong><small>{account.remark || account.name || '无备注'}</small></div></div></td>
                  <td><strong>{tierLabel(account.user_paygate_tier)}</strong><div className="flow-pool__credit">额度 {account.credits ?? '—'}</div>{account.ban_reason && <small className="bad">{account.ban_reason}</small>}</td>
                  <td><div className="flow-pool__chips"><span className={account.video_enabled ? 'on' : 'off'}>视频 {account.video_enabled ? account.video_concurrency : '关'}</span><span className={account.image_enabled ? 'on' : 'off'}>图片 {account.image_enabled ? account.image_concurrency : '关'}</span></div><small>{account.is_active ? '管理员已启用' : '管理员已暂停'} · {routable ? '可调度' : '不可调度'}</small></td>
                  <td><div className={routable ? 'good' : 'bad'}>AT：{authorizationLabel(account)}</div><small>到期：{time(account.at_expires)}</small><small>HTTP 保活：{account.keepalive_enabled ? `已开启${account.last_keepalive_success_at ? ` · 最近成功 ${time(account.last_keepalive_success_at)}` : ''}` : '未开启'}</small><small>固定代理：{account.proxy_name || proxyMap[Number(account.proxy_id)]?.name || account.captcha_proxy_url || '未绑定'}</small><small>Windows Profile：{account.browser_profile_id || '未绑定'}</small><small>浏览器指纹：{account.browser_fingerprint_state === 'captured' ? '已采集' : '缺失'}</small>{currentFailure && <small className="bad">{currentFailure}</small>}{account.last_keepalive_error && <small className="bad">保活：{account.last_keepalive_error}</small>}</td>
                  <td><div>视频 {account.video_count || 0} · 图片 {account.image_count || 0}</div><small>最近使用：{time(account.last_used_at)}</small><small>错误 {account.error_count || 0}</small></td>
                  <td><div className="flow-pool__row-actions"><button type="button" className="btn btn--sm" disabled={action.isPending} onClick={() => openEdit(account)}>配置</button><button type="button" className="btn btn--sm" disabled={action.isPending} onClick={() => action.mutate({ fn: () => flowAccountApi.refreshCredits(account.id), success: '额度已刷新' })}>刷新额度</button><button type="button" className="btn btn--sm" disabled={action.isPending || !account.browser_profile_id} onClick={() => reauthAccount(account)}>浏览器重新登录</button><button type="button" className={`btn btn--sm${account.is_active ? ' btn--danger' : ''}`} disabled={action.isPending} onClick={() => toggleAccount(account)}>{account.is_active ? '暂停' : '启用'}</button><button type="button" className="btn btn--sm btn--danger" disabled={action.isPending || account.is_active} onClick={() => removeAccount(account)}>删除</button></div></td>
                </tr>
            })}
                {!tokens.length && <tr><td colSpan="6" className="empty">号池尚无账号。点击“添加账号”完成首次接入。</td></tr>}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <Modal open={importOpen} title="添加 Flow 账号" onClose={() => { void closeImport() }}>
        <form className="flow-pool__form" onSubmit={submitImport}>
          <label><span>登录设备</span><select className="input" required value={importForm.device_id} onChange={(event) => setImportForm({ ...importForm, device_id: event.target.value })}><option value="">请选择在线设备</option>{onlineDevices.map((device) => <option key={device.device_id} value={device.device_id} disabled={Number(device.profile_available_count) <= 0}>{device.device_name} · {device.profile_used_count}/{device.profile_capacity} Profile{Number(device.profile_available_count) <= 0 ? '（已满）' : ''}</option>)}</select><small>每个账号都会绑定该设备上的独立 Slot 和固定 Chrome Profile。</small></label>
          <label><span>账号固定代理</span><select className="input" required value={importForm.proxy_id} onChange={(event) => setImportForm({ ...importForm, proxy_id: Number(event.target.value) || '' })}><option value="">请选择代理</option>{activeProxies.map((proxy) => <option key={proxy.id} value={proxy.id}>{proxy.name} · {proxy.display_url}</option>)}</select><small>一个代理可供多个账号使用；该账号登录、保活和 Flow 请求始终使用此代理。</small></label>
          <label><span>备注</span><input className="input" maxLength="500" value={importForm.remark} onChange={(event) => setImportForm({ ...importForm, remark: event.target.value })} placeholder="例如：Flow 主账号 01" /></label>
          <div className="flow-pool__form-grid"><label className="flow-pool__check"><input type="checkbox" checked={importForm.video_enabled} onChange={(event) => setImportForm({ ...importForm, video_enabled: event.target.checked })} />启用视频生成</label><label><span>视频并发</span><input className="input" type="number" min="-1" max="32" value={importForm.video_concurrency} onChange={(event) => setImportForm({ ...importForm, video_concurrency: event.target.value })} /></label><label className="flow-pool__check"><input type="checkbox" checked={importForm.image_enabled} onChange={(event) => setImportForm({ ...importForm, image_enabled: event.target.checked })} />启用图片生成</label><label><span>图片并发</span><input className="input" type="number" min="-1" max="32" value={importForm.image_concurrency} onChange={(event) => setImportForm({ ...importForm, image_concurrency: event.target.value })} /></label></div>
          {activeSession && <div className={`flow-pool__session flow-pool__session--${activeSession.state}`}><strong>{activeSession.state === 'ready' ? '账号已加入号池' : activeSession.state === 'capture_pending' ? '正在自动采集' : '等待浏览器登录'}</strong><span>{activeSession.message || '请在自动打开的正常 Chrome 中，只从 Flow 网页进入 Google 登录；不要开启 Chrome 浏览器同步。确认 Flow 可访问后关闭整个窗口。'}</span>{activeSession.browser_status === 'login_complete' && <small>已检测到登录窗口关闭，正在切换到自动采集模式。</small>}{activeSession.error && <small className="bad">{activeSession.error}</small>}</div>}
          <div className="flow-pool__modal-actions">
            {isImportSessionComplete(activeSession) ? (
              <button type="button" className="btn btn--primary" disabled={closingImport} onClick={() => { void closeImport() }}>
                {closingImport ? '正在关闭…' : '完成并关闭'}
              </button>
            ) : (
              <>
                <button type="button" className="btn" disabled={closingImport} onClick={() => { void closeImport() }}>{closingImport ? '正在关闭…' : '关闭并停止浏览器'}</button>
                <button type="submit" className="btn btn--primary" disabled={action.isPending || closingImport || !importForm.device_id || ['awaiting_login', 'capture_pending', 'keepalive_pending'].includes(activeSession?.state)}>{action.isPending ? '正在启动…' : '打开 Chrome 登录'}</button>
              </>
            )}
          </div>
        </form>
      </Modal>

      <Modal
        open={reauthOpen}
        title={`重新授权${reauthTarget?.email ? ` · ${reauthTarget.email}` : ''}`}
        onClose={() => { void closeReauth() }}
      >
        <div className="flow-pool__form">
          <div className="flow-pool__session">
            <strong>
              {reauthSession?.state === 'ready'
                ? '重新授权完成'
                : reauthSession?.state === 'failed'
                  ? '重新授权未通过'
                  : reauthSession?.state === 'capture_pending'
                    ? '正在隐藏采集登录态'
                    : '等待浏览器登录'}
            </strong>
            <span>{reauthSession?.message || '正在启动该账号原来的固定 Chrome Profile…'}</span>
            {reauthSession?.error && <small className="bad">{reauthSession.error}</small>}
          </div>
          <div className="small-muted">
            请在打开的 Chrome 中完成 Google 登录和账号验证，然后进入 Flow 打开或创建一个项目。
            确认 Flow 可正常使用后关闭整个 Chrome 窗口；后续登录态采集会在后台隐藏完成。
          </div>
          <div className="flow-pool__modal-actions">
            <button type="button" className="btn btn--primary" disabled={closingReauth || action.isPending} onClick={() => { void closeReauth() }}>
              {closingReauth
                ? '正在停止…'
                : ['ready', 'failed', 'cancelled'].includes(reauthSession?.state)
                  ? '关闭'
                  : '关闭并停止浏览器'}
            </button>
          </div>
        </div>
      </Modal>

      <Modal open={!!editAccount} title="配置 Flow 账号" onClose={() => !action.isPending && setEditAccount(null)}>
        {editForm && <form className="flow-pool__form" onSubmit={submitEdit}><label><span>备注</span><input className="input" maxLength="500" value={editForm.remark} onChange={(event) => setEditForm({ ...editForm, remark: event.target.value })} /></label><label><span>账号固定代理</span><select className="input" required value={editForm.proxy_id} onChange={(event) => setEditForm({ ...editForm, proxy_id: Number(event.target.value) || '' })}><option value="">请选择代理</option>{activeProxies.map((proxy) => <option key={proxy.id} value={proxy.id}>{proxy.name} · {proxy.display_url}</option>)}</select><small>修改后该账号的独立浏览器运行时会在下一次任务前重建。</small></label><div className="flow-pool__form-grid"><label className="flow-pool__check"><input type="checkbox" checked={editForm.video_enabled} onChange={(event) => setEditForm({ ...editForm, video_enabled: event.target.checked })} />启用视频生成</label><label><span>视频并发</span><input className="input" type="number" min="-1" max="32" value={editForm.video_concurrency} onChange={(event) => setEditForm({ ...editForm, video_concurrency: event.target.value })} /></label><label className="flow-pool__check"><input type="checkbox" checked={editForm.image_enabled} onChange={(event) => setEditForm({ ...editForm, image_enabled: event.target.checked })} />启用图片生成</label><label><span>图片并发</span><input className="input" type="number" min="-1" max="32" value={editForm.image_concurrency} onChange={(event) => setEditForm({ ...editForm, image_concurrency: event.target.value })} /></label></div><div className="flow-pool__modal-actions"><button type="button" className="btn" onClick={() => setEditAccount(null)}>取消</button><button type="submit" className="btn btn--primary" disabled={action.isPending}>保存配置</button></div></form>}
      </Modal>

      <Modal open={proxyOpen} title={editingProxy ? '编辑代理' : '添加代理'} onClose={() => !action.isPending && setProxyOpen(false)}>
        <form className="flow-pool__form" onSubmit={submitProxy}>
          <label><span>代理名称</span><input className="input" required maxLength="128" value={proxyForm.name} onChange={(event) => setProxyForm({ ...proxyForm, name: event.target.value })} placeholder="例如：美国出口 01" /></label>
          <label><span>代理地址{editingProxy ? '（留空表示不修改）' : ''}</span><input className="input" required={!editingProxy} value={proxyForm.proxy_url} onChange={(event) => setProxyForm({ ...proxyForm, proxy_url: event.target.value })} placeholder="socks5h://host:port 或 http://user:pass@host:port" autoComplete="off" /><small>凭据仅在服务端加密保存，页面和日志只显示脱敏后的主机与端口。</small></label>
          <label className="flow-pool__check"><input type="checkbox" checked={proxyForm.is_active} onChange={(event) => setProxyForm({ ...proxyForm, is_active: event.target.checked })} />启用此代理</label>
          <div className="flow-pool__modal-actions"><button type="button" className="btn" onClick={() => setProxyOpen(false)}>取消</button><button type="submit" className="btn btn--primary" disabled={action.isPending}>{action.isPending ? '保存中…' : '保存代理'}</button></div>
        </form>
      </Modal>
    </div>
  )
}
