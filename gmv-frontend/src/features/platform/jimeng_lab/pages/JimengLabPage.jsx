import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Loading from '../../../../components/ui/Loading.jsx'
import jimengLabApi from '../service.js'
import './JimengLabPage.css'

const DEFAULT_PROMPT = '夜晚的现代城市天台，微风吹动一盏暖色小灯，镜头快速向前推进后平稳停下，真实自然的光影变化，竖屏短视频。'

function errorText(error) {
  return error?.response?.data?.detail || error?.message || '操作失败，请稍后重试。'
}

function time(value) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function stateLabel(value) {
  return {
    awaiting_login: '等待登录',
    capture_pending: '正在采集',
    ready: '会话有效',
    failed: '验证失败',
    cancelled: '已取消',
  }[value] || value || '未知'
}

export default function JimengLabPage() {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState(null)
  const [deviceId, setDeviceId] = useState('')
  const [proxyId, setProxyId] = useState('')
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT)
  const [model, setModel] = useState('jimeng-video-seedance-2.0-fast')
  const query = useQuery({
    queryKey: ['platform', 'jimeng-lab'],
    queryFn: jimengLabApi.overview,
    staleTime: 3_000,
    refetchInterval: (current) => {
      const sessions = current.state.data?.sessions || []
      const busy = sessions.some((item) => ['awaiting_login', 'capture_pending'].includes(item.state)
        || ['queued', 'running', 'waiting_upstream'].includes(item.test?.state))
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
  const selectedProxy = proxyId || proxies[0]?.id || ''

  const action = useMutation({
    mutationFn: async ({ fn }) => fn(),
    onSuccess: async (_result, variables) => {
      setMessage({ type: 'success', text: variables.success })
      await queryClient.invalidateQueries({ queryKey: ['platform', 'jimeng-lab'] })
    },
    onError: (error) => setMessage({ type: 'error', text: errorText(error) }),
  })

  const openLogin = () => {
    setMessage(null)
    action.mutate({
      fn: () => jimengLabApi.startBrowserSession({ device_id: selectedDevice, proxy_id: Number(selectedProxy) }),
      success: '已打开即梦专用 Chrome Profile，请登录并确认视频页可访问后关闭整个窗口。',
    })
  }

  const cancelLogin = () => {
    if (!activeSession?.session_id) return
    action.mutate({
      fn: () => jimengLabApi.cancelBrowserSession(activeSession.session_id),
      success: '即梦实验登录已取消，专用浏览器会自动关闭。',
    })
  }

  const verify = () => {
    if (!activeSession?.session_id) return
    action.mutate({
      fn: () => jimengLabApi.verifySession(activeSession.session_id),
      success: '即梦登录态验证完成。',
    })
  }

  const generate = (event) => {
    event.preventDefault()
    if (!activeSession?.session_id) return
    action.mutate({
      fn: () => jimengLabApi.createTest(activeSession.session_id, { prompt, model }),
      success: '4 秒 Seedance 测试任务已进入后台队列。',
    })
  }

  if (query.isLoading) return <Loading text="正在读取即梦实验环境…" />

  return (
    <div className="jimeng-lab">
      <header className="page-header jimeng-lab__header">
        <div>
          <h1 className="page-title">即梦实验</h1>
          <div className="small-muted">平台管理员专属 · 独立浏览器 Profile · 加密登录态 · 不进入正式供应商路由</div>
        </div>
        <button type="button" className="btn" disabled={query.isFetching} onClick={() => query.refetch()}>刷新状态</button>
      </header>

      {message && <div className={`alert alert--${message.type === 'error' ? 'error' : 'success'}`}>{message.text}</div>}
      {query.error && <div className="alert alert--error">{errorText(query.error)}</div>}

      <section className="jimeng-lab__summary">
        <article><span>实验服务</span><strong className={overview.service?.status === 'healthy' ? 'good' : 'bad'}>{overview.service?.status === 'healthy' ? '运行正常' : '不可用'}</strong><small>仅监听服务器回环地址</small></article>
        <article><span>浏览器设备</span><strong>{devices.length}</strong><small>在线且已绑定</small></article>
        <article><span>账号状态</span><strong>{stateLabel(activeSession?.state)}</strong><small>{activeSession?.credential_state === 'encrypted' ? '凭据已加密' : '尚未采集'}</small></article>
        <article><span>测试规格</span><strong>4 秒 · 9:16</strong><small>Seedance 2.0 / Fast</small></article>
      </section>

      <section className="card jimeng-lab__card">
        <div className="card__header">第一步：打开专用 Chrome 登录即梦</div>
        <div className="jimeng-lab__notice">
          <strong>安全边界</strong>
          <span>系统不会自动填写账号、绕过验证码或处理 MFA。请你在普通 Chrome 中手动完成登录；确认即梦视频生成页面可用后，关闭整个窗口。Bridge 会用同一固定 Profile 短暂采集必要 Cookie，Cookie 不会返回前端。</span>
        </div>
        <div className="jimeng-lab__grid">
          <label><span>Windows 设备</span><select className="input" value={selectedDevice} onChange={(event) => setDeviceId(event.target.value)}><option value="">请选择在线设备</option>{devices.map((item) => <option key={item.device_id} value={item.device_id}>{item.device_name} · {item.device_id}</option>)}</select></label>
          <label><span>固定代理</span><select className="input" value={selectedProxy} onChange={(event) => setProxyId(event.target.value)}><option value="">请选择代理</option>{proxies.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.display_url}</option>)}</select></label>
        </div>
        <div className="jimeng-lab__actions">
          <button type="button" className="btn btn--primary" disabled={action.isPending || !selectedDevice || !selectedProxy || ['awaiting_login', 'capture_pending'].includes(activeSession?.state)} onClick={openLogin}>打开 Chrome 登录</button>
          <button type="button" className="btn" disabled={action.isPending || !['awaiting_login', 'capture_pending'].includes(activeSession?.state)} onClick={cancelLogin}>取消登录</button>
          <button type="button" className="btn" disabled={action.isPending || activeSession?.state !== 'ready'} onClick={verify}>验证登录态</button>
        </div>
        {activeSession && <div className={`jimeng-lab__session jimeng-lab__session--${activeSession.state}`}><strong>{stateLabel(activeSession.state)}</strong><span>{activeSession.message || '等待状态更新…'}</span><small>Profile：{activeSession.profile_id || '登录完成后绑定'} · 最近验证：{time(activeSession.last_verified_at)}</small>{activeSession.error && <small className="bad">{activeSession.error}</small>}</div>}
        {!devices.length && <p className="small-muted">当前没有在线 Hermes Bridge 设备。请先运行现有 Windows 浏览器桥，再刷新本页。</p>}
        {!proxies.length && <p className="small-muted">当前没有启用的平台代理。请先到“Flow 视频号池”维护代理。</p>}
      </section>

      <form className="card jimeng-lab__card" onSubmit={generate}>
        <div className="card__header">第二步：生成 4 秒 Seedance 测试视频</div>
        <div className="jimeng-lab__grid">
          <label><span>模型</span><select className="input" value={model} onChange={(event) => setModel(event.target.value)}><option value="jimeng-video-seedance-2.0-fast">Seedance 2.0 Fast</option><option value="jimeng-video-seedance-2.0">Seedance 2.0</option></select></label>
          <label><span>固定参数</span><input className="input" value="4 秒 · 720p · 9:16 · 文生视频" disabled /></label>
        </div>
        <label className="jimeng-lab__prompt"><span>测试提示词</span><textarea className="input" required minLength="3" maxLength="2000" rows="5" value={prompt} onChange={(event) => setPrompt(event.target.value)} /></label>
        <div className="jimeng-lab__actions"><button type="submit" className="btn btn--primary" disabled={action.isPending || activeSession?.state !== 'ready' || ['queued', 'running', 'waiting_upstream', 'upstream_timeout'].includes(activeSession?.test?.state)}>生成测试视频</button></div>
        {activeSession?.test && activeSession.test.state !== 'idle' && <div className={`jimeng-lab__test jimeng-lab__test--${activeSession.test.state}`}><strong>任务：{activeSession.test.state}</strong><span>{activeSession.test.message}</span>{activeSession.test.error && <small className="bad">{activeSession.test.error}</small>}{activeSession.test.video_url && <video controls playsInline src={activeSession.test.video_url}>浏览器不支持视频播放。</video>}</div>}
      </form>
    </div>
  )
}
