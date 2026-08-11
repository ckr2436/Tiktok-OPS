import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Loading from '../../../../components/ui/Loading.jsx'
import doubaoLabApi from '../service.js'
import './DoubaoLabPage.css'

const DEFAULT_PROMPT = '一只橘猫站在雨后的霓虹街道上，回头看向镜头，微风吹动毛发，镜头快速推近后平稳停下，竖屏短视频。'

function errorText(error) {
  return error?.response?.data?.detail || error?.message || '操作失败，请稍后重试。'
}

function time(value) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function stateLabel(value) {
  return {
    awaiting_login: '等待网页登录',
    capture_pending: '正在采集登录态',
    ready: '登录资料已采集',
    captcha_required: '等待人工验证',
    failed: '验证失败',
    cancelled: '已取消',
  }[value] || value || '尚未登录'
}

export function capacityLabel(capacity) {
  const state = String(capacity?.state || 'unknown')
  if (state === 'available') return '最近生成验证可用'
  if (state === 'exhausted') return `额度暂不可用 · ${time(capacity?.retry_at)} 后重新探测`
  return '官网未提供精确额度 · 按实际成功状态调度'
}

export function accountProxyLabel(session, proxies) {
  if (session?.network_mode === 'direct') return '设备本地直连'
  const proxy = (proxies || []).find((item) => Number(item.id) === Number(session?.proxy_id))
  if (!proxy) return session?.proxy_id ? `未知代理 #${session.proxy_id}` : '未绑定代理'
  return `${proxy.name} · ${proxy.display_url}${proxy.is_active ? '' : ' · 已停用'}`
}

export function membershipLabel(membership) {
  if (membership?.tier === 'enhanced') return '加强套餐 · 最长 15 秒'
  return '免费账号 · 最长 10 秒'
}

export function capabilityLabel(capability) {
  const state = String(capability?.state || 'unknown')
  return {
    ready: 'Seedance 编辑器已验证',
    probing: '正在检测 Seedance 能力',
    captcha_required: '需要人工完成 CAPTCHA',
    auth_required: '登录态已失效',
    region_restricted: '当前代理区域不可用',
    unavailable: '当前账号未开放视频编辑器',
    rate_limited: '账号风控冷却中',
    unknown: '尚未验证视频能力',
  }[state] || state
}

export function poolStatusLabel(pool) {
  return {
    production_ready: '生产可用',
    busy: '正在执行任务',
    auth_required: '登录已失效',
    captcha_required: '等待人工验证',
    auth_check_due: '待认证探测',
    region_restricted: '网络区域不可用',
    capacity_exhausted: '额度冷却中',
    cooling_down: '故障冷却中',
    capability_check_due: '登录有效 · 待视频能力验证',
    disabled: '已停用',
    device_offline: '设备离线',
    device_recovering: '设备恢复中',
  }[String(pool?.status || '')] || '状态待确认'
}

export function authenticationLabel(authentication) {
  const state = String(authentication?.state || 'unknown')
  if (state === 'authenticated' && authentication?.fresh) return '已登录'
  if (state === 'authenticated') return '认证已过期，等待复查'
  if (state === 'auth_required') return '登录已失效'
  return '尚未完成强认证探测'
}

export function networkLabel(network) {
  return {
    reachable: '可访问',
    region_restricted: '区域受限',
    unreachable: '暂不可达',
    unknown: '待探测',
  }[String(network?.state || 'unknown')] || '待探测'
}

export default function DoubaoLabPage() {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState(null)
  const [deviceId, setDeviceId] = useState('')
  const [proxyId, setProxyId] = useState('direct')
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT)
  const [duration, setDuration] = useState(4)
  const [ratio, setRatio] = useState('9:16')
  const [accountProxyIds, setAccountProxyIds] = useState({})
  const query = useQuery({
    queryKey: ['platform', 'doubao-lab'],
    queryFn: doubaoLabApi.overview,
    staleTime: 3_000,
    refetchInterval: (current) => {
      const sessions = current.state.data?.sessions || []
      const busy = sessions.some((item) => ['awaiting_login', 'capture_pending'].includes(item.state)
        || ['queued', 'running'].includes(item.test?.state)
        || ['preparing', 'awaiting_human'].includes(item.manual_verification?.state)
        || item.pool?.capability?.state === 'probing')
      return busy ? 3_000 : 20_000
    },
  })
  const overview = query.data || {}
  const devices = (overview.devices || []).filter((item) => item.online && item.bound)
  const proxies = (overview.proxies || []).filter((item) => item.is_active)
  const sessions = overview.sessions || []
  const activeSession = useMemo(
    () => sessions.find((item) => ['awaiting_login', 'capture_pending'].includes(item.state))
      || sessions.find((item) => item.state === 'ready')
      || sessions[0],
    [sessions],
  )
  const selectedDevice = deviceId || devices[0]?.device_id || ''
  const selectedProxy = proxyId || 'direct'
  const testDurations = activeSession?.membership?.allowed_durations_seconds
    || overview.capabilities?.durations
    || [4]
  const selectedDuration = testDurations.includes(Number(duration))
    ? Number(duration)
    : Number(testDurations[0] || 4)

  const action = useMutation({
    mutationFn: async ({ fn }) => fn(),
    onSuccess: async (_result, variables) => {
      setMessage({ type: 'success', text: variables.success })
      await queryClient.invalidateQueries({ queryKey: ['platform', 'doubao-lab'] })
    },
    onError: (error) => setMessage({ type: 'error', text: errorText(error) }),
  })

  const openLogin = () => {
    setMessage(null)
    action.mutate({
      fn: () => doubaoLabApi.startBrowserSession({
        device_id: selectedDevice,
        proxy_id: selectedProxy === 'direct' ? null : Number(selectedProxy),
      }),
      success: '已打开豆包专用 Chrome Profile。请在官方网页完成登录，确认聊天页可用后关闭整个窗口。',
    })
  }

  const cancelLogin = () => {
    if (!activeSession?.session_id) return
    action.mutate({
      fn: () => doubaoLabApi.cancelBrowserSession(activeSession.session_id),
      success: '豆包实验登录已取消；独立 Profile 会保留供下次继续使用。',
    })
  }

  const verify = () => {
    if (!activeSession?.session_id) return
    action.mutate({
      fn: () => doubaoLabApi.verifySession(activeSession.session_id),
      success: '豆包登录态检查完成。',
    })
  }

  const probe = (session) => {
    action.mutate({
      fn: () => doubaoLabApi.probeSession(session.session_id),
      success: 'Seedance 视频能力检测已进入后台队列。',
    })
  }

  const generate = (event) => {
    event.preventDefault()
    if (!activeSession?.session_id) return
    action.mutate({
      fn: () => doubaoLabApi.createTest(activeSession.session_id, { prompt, duration: selectedDuration, ratio }),
      success: 'Seedance 2.0 Mini 测试任务已进入后台队列。',
    })
  }

  const readyAccounts = sessions.filter((item) => item.pool?.ready)

  const togglePool = (session) => {
    action.mutate({
      fn: () => doubaoLabApi.updatePoolState(session.session_id, !session.pool?.enabled),
      success: session.pool?.enabled ? '账号已从生产号池停用。' : '账号已加入生产号池。',
    })
  }

  const changeMembership = (session, tier) => {
    action.mutate({
      fn: () => doubaoLabApi.updateMembership(session.session_id, tier),
      success: tier === 'enhanced'
        ? '账号已标记为加强套餐，可调度 4–15 秒。'
        : '账号已标记为免费账号，已收紧为 4–10 秒。',
    })
  }

  const relogin = (session) => {
    action.mutate({
      fn: () => doubaoLabApi.relogin(session.session_id),
      success: '已在该账号原有独立 Profile 中打开重新登录流程。',
    })
  }

  const startManualVerification = (session) => {
    action.mutate({
      fn: () => doubaoLabApi.startManualVerification(session.session_id),
      success: '系统正在固定 Profile 中打开 AI 创作，并预设 Seedance 2.0 Mini、9:16、4 秒和验证提示词。准备完成后请直接点击发送并完成人工验证。',
    })
  }

  const completeManualVerification = (session) => {
    action.mutate({
      fn: () => doubaoLabApi.completeManualVerification(session.session_id),
      success: '系统已检查当前页面；进入真实 Seedance 生成会话后，账号会自动恢复生产能力。',
    })
  }

  const reconcilePool = () => {
    action.mutate({
      fn: () => doubaoLabApi.reconcilePool(),
      success: '号池状态已清洗：地区限制、登录失效与 CAPTCHA 已分别归类。',
    })
  }

  const changeProxy = (session) => {
    const currentValue = session.network_mode === 'direct' ? 'direct' : String(session.proxy_id || '')
    const nextValue = String(accountProxyIds[session.session_id] || currentValue)
    if (!nextValue || nextValue === currentValue) return
    action.mutate({
      fn: () => doubaoLabApi.updateProxy(
        session.session_id,
        nextValue === 'direct' ? null : Number(nextValue),
      ),
      success: nextValue === 'direct'
        ? '已切换为设备本地直连，请在原有独立 Profile 中重新确认登录。'
        : '代理已更换，已在该账号原有独立 Profile 中打开重新登录流程。',
    })
  }

  const deleteAccount = (session) => {
    if (!window.confirm('确认从豆包号池删除这个账号？独立浏览器 Profile 将被封存且不再复用；历史视频和审计记录会保留。')) return
    action.mutate({
      fn: () => doubaoLabApi.deleteAccount(session.session_id),
      success: '账号已从豆包号池删除，原 Profile 已安全封存。',
    })
  }

  if (query.isLoading) return <Loading text="正在读取豆包 Seedance 号池…" />

  return (
    <div className="doubao-lab">
      <header className="page-header doubao-lab__header">
        <div>
          <h1 className="page-title">豆包 Seedance 号池</h1>
          <div className="small-muted">平台管理员专属 · 独立账号 Profile · 可选本地直连或固定代理 · 后台强认证探测 · 统一动态路由优先供应商</div>
        </div>
        <div className="doubao-lab__actions">
          <button type="button" className="btn" disabled={action.isPending} onClick={reconcilePool}>清洗号池状态</button>
          <button type="button" className="btn" disabled={query.isFetching} onClick={() => query.refetch()}>刷新状态</button>
        </div>
      </header>

      {message && <div className={`alert alert--${message.type === 'error' ? 'error' : 'success'}`}>{message.text}</div>}
      {query.error && <div className="alert alert--error">{errorText(query.error)}</div>}

      <section className="doubao-lab__summary">
        <article><span>生产链路</span><strong className={overview.service?.status === 'healthy' ? 'good' : 'bad'}>{overview.service?.status === 'healthy' ? '运行正常' : '不可用'}</strong><small>Seedance 2.0 Mini · 优先级 1</small></article>
        <article><span>浏览器设备</span><strong>{devices.length}</strong><small>在线且已绑定</small></article>
        <article><span>可用账号</span><strong>{readyAccounts.length} / {sessions.length}</strong><small>单账号单租约，额度耗尽自动轮换</small></article>
        <article><span>生成能力</span><strong>{overview.capabilities?.durations?.length ? `${Math.min(...overview.capabilities.durations)}–${Math.max(...overview.capabilities.durations)} 秒` : '4–10 秒'}</strong><small>按号池账号等级动态收紧</small></article>
      </section>

      <section className="card doubao-lab__card">
        <div className="card__header">第一步：打开独立 Chrome 登录豆包</div>
        <div className="doubao-lab__notice">
          <strong>每次添加都会创建新的独立账号 Profile</strong>
          <span>豆包建议使用设备本地直连；如选择代理，账号会固定到该代理且不与其他 Profile 混用。系统不会代替用户完成短信验证码或 CAPTCHA。登录成功并关闭整个窗口后，Bridge 会加密采集登录资料；后台认证探测只走 HTTP，不会自行弹出浏览器或消耗视频额度。</span>
        </div>
        <div className="doubao-lab__grid">
          <label><span>Windows 设备</span><select className="input" value={selectedDevice} onChange={(event) => setDeviceId(event.target.value)}><option value="">请选择在线设备</option>{devices.map((item) => <option key={item.device_id} value={item.device_id}>{item.device_name} · {item.device_id}</option>)}</select></label>
          <label><span>网络出口</span><select className="input" value={selectedProxy} onChange={(event) => setProxyId(event.target.value)}><option value="direct">设备本地直连（豆包推荐）</option>{proxies.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.display_url}</option>)}</select></label>
        </div>
        <div className="doubao-lab__actions">
          <button type="button" className="btn btn--primary" disabled={action.isPending || !selectedDevice || ['awaiting_login', 'capture_pending'].includes(activeSession?.state)} onClick={openLogin}>添加豆包账号</button>
          <button type="button" className="btn" disabled={action.isPending || !['awaiting_login', 'capture_pending'].includes(activeSession?.state)} onClick={cancelLogin}>取消登录</button>
          <button type="button" className="btn" disabled={action.isPending || activeSession?.state !== 'ready'} onClick={verify}>检查登录态</button>
        </div>
        {activeSession && <div className={`doubao-lab__session doubao-lab__session--${activeSession.state}`}><strong>{stateLabel(activeSession.state)}</strong><span>{activeSession.message || '等待状态更新…'}</span><small>Profile：{activeSession.profile_id || '登录完成后绑定'} · 最近检查：{time(activeSession.last_verified_at)}</small>{activeSession.error && <small className="bad">{activeSession.error}</small>}</div>}
        {!devices.length && <p className="small-muted">当前没有在线 Hermes Bridge 设备。请先运行 Windows 浏览器桥，再刷新本页。</p>}
        {!proxies.length && <p className="small-muted">当前没有启用的平台代理。请先到“Flow 视频号池”维护代理。</p>}
      </section>

      <section className="card doubao-lab__card">
        <div className="card__header">生产账号池</div>
        {!sessions.length && <p className="small-muted">还没有账号。添加并完成网页登录后，账号会出现在这里。</p>}
        <div className="doubao-lab__accounts">
          {sessions.map((session) => <article className="doubao-lab__account" key={session.session_id}>
            <div><strong>{session.profile_id || session.session_id}</strong><span className={session.pool?.ready ? 'good' : 'small-muted'}>{poolStatusLabel(session.pool)}</span></div>
            <small>网络出口：{accountProxyLabel(session, overview.proxies)} · 登录检查 {time(session.last_verified_at)}</small>
            <small>账号等级：{membershipLabel(session.membership)} · 历史账号默认按免费账号处理</small>
            <small>登录认证：{authenticationLabel(session.pool?.authentication)} · 最近 {time(session.pool?.authentication?.checked_at)} · 下次 {time(session.pool?.authentication?.next_check_at)}</small>
            <small>网络探测：{networkLabel(session.pool?.network)} · 检测 {time(session.pool?.network?.checked_at)}</small>
            <small>视频能力：{capabilityLabel(session.pool?.capability)} · 检测 {time(session.pool?.capability?.checked_at)}</small>
            <small>首次登录后系统会自动做一次免额度能力检测；下方按钮仅用于人工复检，后台登录保活不会反复打开浏览器。</small>
            <small>容量：{capacityLabel(session.pool?.capacity)} · 最近成功 {time(session.pool?.last_success_at)}</small>
            <small>成功：{time(session.pool?.last_success_at)} · 连续错误 {session.pool?.consecutive_errors || 0}{session.pool?.last_error ? ` · ${session.pool.last_error}` : ''}</small>
            {session.manual_verification?.state !== 'idle' && <small>人工验证：{session.manual_verification?.message || session.manual_verification?.state}</small>}
            <div className="doubao-lab__proxy-editor">
              <select className="input" aria-label={`更换 ${session.profile_id || session.session_id} 的网络出口`} value={accountProxyIds[session.session_id] || (session.network_mode === 'direct' ? 'direct' : session.proxy_id) || ''} onChange={(event) => setAccountProxyIds((current) => ({ ...current, [session.session_id]: event.target.value }))}>
                <option value="direct">设备本地直连（豆包推荐）</option>
                {(overview.proxies || []).map((item) => <option key={item.id} value={item.id} disabled={!item.is_active}>{item.name} · {item.display_url}{item.is_active ? '' : ' · 已停用'}</option>)}
              </select>
              <button type="button" className="btn" disabled={action.isPending || session.pool?.lease_task_id || !accountProxyIds[session.session_id]} onClick={() => changeProxy(session)}>更换出口并重新登录</button>
            </div>
            <div className="doubao-lab__proxy-editor">
              <select
                className="input"
                aria-label={`设置 ${session.profile_id || session.session_id} 的账号等级`}
                value={session.membership?.tier || 'free'}
                disabled={action.isPending || Boolean(session.pool?.lease_task_id)}
                onChange={(event) => changeMembership(session, event.target.value)}
              >
                <option value="free">免费账号（4–10 秒）</option>
                <option value="enhanced">加强套餐（4–15 秒）</option>
              </select>
              <span className="small-muted">等级由管理员确认；网页登录态不会可靠暴露订阅类型。</span>
            </div>
            <div className="doubao-lab__actions">
              <button type="button" className="btn" disabled={action.isPending || session.state !== 'ready' || session.pool?.lease_task_id} onClick={() => probe(session)}>重新检测视频能力</button>
              <button type="button" className="btn" disabled={action.isPending || session.state !== 'ready'} onClick={() => togglePool(session)}>{session.pool?.enabled ? '停用账号' : '加入号池'}</button>
              <button
                type="button"
                className="btn"
                disabled={action.isPending
                  || (session.state !== 'captcha_required' && session.pool?.capability?.state !== 'captcha_required')
                  || (session.pool?.lease_task_id && !String(session.pool.lease_task_id).startsWith('manual-capture:'))}
                onClick={() => startManualVerification(session)}
              >打开人工验证</button>
              <button
                type="button"
                className="btn btn--primary"
                disabled={action.isPending || !String(session.pool?.lease_task_id || '').startsWith('manual-capture:')}
                onClick={() => completeManualVerification(session)}
              >检查验证结果</button>
              <button type="button" className="btn" disabled={action.isPending || session.pool?.lease_task_id} onClick={() => relogin(session)}>重新登录</button>
              <button type="button" className="btn doubao-lab__danger" disabled={action.isPending || session.pool?.lease_task_id || ['queued', 'running'].includes(session.test?.state)} onClick={() => deleteAccount(session)}>删除账号</button>
            </div>
          </article>)}
        </div>
      </section>

      <form className="card doubao-lab__card" onSubmit={generate}>
        <div className="card__header">链路验收：生成 Seedance 2.0 Mini 测试视频</div>
        <div className="doubao-lab__grid">
          <label><span>时长</span><select className="input" value={selectedDuration} onChange={(event) => setDuration(Number(event.target.value))}>{testDurations.map((value) => <option key={value} value={value}>{value} 秒</option>)}</select></label>
          <label><span>画面比例</span><select className="input" value={ratio} onChange={(event) => setRatio(event.target.value)}>{(overview.capabilities?.ratios || ['9:16']).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        </div>
        <label className="doubao-lab__prompt"><span>测试提示词</span><textarea className="input" required minLength="3" maxLength="495" rows="5" value={prompt} onChange={(event) => setPrompt(event.target.value)} /><small>{prompt.length}/495 字符；豆包页面输入硬上限 500，系统按已验证的 495 字符安全预算提交，不会写入重复模式指令或静默截断。</small></label>
        <div className="doubao-lab__actions"><button type="submit" className="btn btn--primary" disabled={action.isPending || activeSession?.state !== 'ready' || ['queued', 'running'].includes(activeSession?.test?.state)}>生成测试视频</button></div>
        {activeSession?.test && activeSession.test.state !== 'idle' && <div className={`doubao-lab__test doubao-lab__test--${activeSession.test.state}`}><strong>任务：{activeSession.test.state}</strong><span>{activeSession.test.message}</span>{activeSession.test.error && <small className="bad">{activeSession.test.error}</small>}{activeSession.test.video_url && <video controls playsInline src={activeSession.test.video_url}>浏览器不支持视频播放。</video>}</div>}
      </form>
    </div>
  )
}
