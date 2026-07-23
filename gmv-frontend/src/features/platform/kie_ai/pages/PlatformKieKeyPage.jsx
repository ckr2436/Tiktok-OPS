import { Fragment, useDeferredValue, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import apiKeyApi from '../service.js'
import Modal from '../../../../components/ui/Modal.jsx'
import Loading from '../../../../components/ui/Loading.jsx'
import './PlatformKieKeyPage.css'

const TABS = [
  ['providers', '供应商与 Key'],
  ['models', '模型目录'],
  ['routes', '路由策略'],
  ['health', '健康与审计'],
]

const PAGE_SIZE = 50
const WORKLOAD_LABELS = {
  default: '默认模型调用',
  video_analyst: '视频内容分析',
}
const CAPABILITY_LABELS = {
  text: '文本',
  multimodal: '多模态',
  image: '图像',
  video: '视频',
  audio: '音频',
}

function Pager({ page, pageSize, total, onPage }) {
  const pages = Math.max(1, Math.ceil((total || 0) / pageSize))
  return (
    <div className="ai-pager">
      <span>共 {total || 0} 条 · 第 {page}/{pages} 页</span>
      <div>
        <button type="button" className="btn btn--sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
        <button type="button" className="btn btn--sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>下一页</button>
      </div>
    </div>
  )
}

function defaultForm(provider = 'bandianwa') {
  return { name: '', api_key: '', provider_key: provider, is_default: false }
}

function fmtTime(value) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function statusText(value) {
  return ({ HEALTHY: '健康', UNKNOWN: '待探测', UNCONFIGURED: '待发现', DEGRADED: '降级', HALF_OPEN: '恢复试探', CIRCUIT_OPEN: '已熔断', DISCOVERED: '已发现', VERIFIED: '已验证', UNAVAILABLE: '已下线', DISABLED: '已停用' })[value] || value || '—'
}

function Status({ value, ok }) {
  const type = ok === true || ['HEALTHY', 'VERIFIED', 'SUCCEEDED'].includes(value) ? 'ok'
    : ok === false || ['CIRCUIT_OPEN', 'FAILED', 'UNAVAILABLE'].includes(value) ? 'bad' : 'warn'
  return <span className={`ai-status ai-status--${type}`}>{statusText(value)}</span>
}

function RouteRole({ rank, enabled, groupSize }) {
  if (!enabled) return <span className="ai-route-role ai-route-role--candidate">候选</span>
  if (groupSize === 1) return <span className="ai-route-role ai-route-role--single">单一路由</span>
  if (rank === 0) return <span className="ai-route-role ai-route-role--primary">主路由</span>
  return <span className="ai-route-role ai-route-role--backup">备用 {rank}</span>
}

export default function PlatformKieKeyPage() {
  const queryClient = useQueryClient()
  const [tab, setTab] = useState('providers')
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(defaultForm())
  const [message, setMessage] = useState(null)
  const [modalError, setModalError] = useState('')
  const [modelSearch, setModelSearch] = useState('')
  const [capability, setCapability] = useState('')
  const [modelPage, setModelPage] = useState(1)
  const [routeSearch, setRouteSearch] = useState('')
  const [routePage, setRoutePage] = useState(1)
  const [routeView, setRouteView] = useState('enabled')
  const deferredModelSearch = useDeferredValue(modelSearch)
  const deferredRouteSearch = useDeferredValue(routeSearch)

  const keysQuery = useQuery({ queryKey: ['platform', 'api-keys'], queryFn: apiKeyApi.listKeys, staleTime: 30_000 })
  const providersQuery = useQuery({ queryKey: ['platform', 'ai-providers'], queryFn: apiKeyApi.listProviders, staleTime: 60_000 })
  const overviewQuery = useQuery({ queryKey: ['platform', 'ai-routing-overview'], queryFn: () => apiKeyApi.getRoutingOverview({ include_details: false }), staleTime: 15_000, refetchInterval: 60_000 })
  const catalogModelsQuery = useQuery({
    queryKey: ['platform', 'ai-catalog-models', modelPage, deferredModelSearch, capability],
    queryFn: () => apiKeyApi.listCatalogModels({ page: modelPage, page_size: PAGE_SIZE, search: deferredModelSearch || undefined, capability: capability || undefined }),
    staleTime: 15_000,
    placeholderData: (previous) => previous,
  })
  const routesQuery = useQuery({
    queryKey: ['platform', 'ai-route-page', routeView, routePage, deferredRouteSearch],
    queryFn: () => apiKeyApi.listRoutes({
      page: routePage,
      page_size: PAGE_SIZE,
      search: deferredRouteSearch || undefined,
      enabled: routeView === 'enabled' ? true : routeView === 'candidates' ? false : undefined,
    }),
    staleTime: 15_000,
    placeholderData: (previous) => previous,
  })
  const providerModelsQuery = useQuery({ queryKey: ['platform', 'ai-provider-models'], queryFn: apiKeyApi.listProviderModels, staleTime: 15_000 })

  const keys = keysQuery.data || []
  const providers = providersQuery.data || []
  const overview = overviewQuery.data || {}
  const summary = overview.summary || {}
  const catalogModels = catalogModelsQuery.data?.items || []
  const routes = routesQuery.data?.items || []
  const enabledRouteCount = summary.enabled_routes ?? summary.eligible_routes ?? 0
  const candidateRouteCount = Math.max(0, (summary.routes || 0) - enabledRouteCount)
  const providerMap = useMemo(() => Object.fromEntries(providers.map((item) => [item.id, item.label])), [providers])
  const keyHealthMap = useMemo(() => Object.fromEntries((overview.key_health || []).map((item) => [item.key_id, item.health_status])), [overview.key_health])
  const routeRankMap = useMemo(() => {
    const groupRanks = {}
    const ranks = {}
    routes.forEach((route) => {
      if (!route.is_enabled) return
      const group = `${route.workload}:${route.logical_model_id}:${route.capability}`
      const rank = groupRanks[group] || 0
      ranks[route.id] = rank
      groupRanks[group] = rank + 1
    })
    return ranks
  }, [routes])
  const routeGroups = useMemo(() => {
    if (routeView !== 'enabled') return [{ key: routeView, routes }]
    const groups = new Map()
    routes.forEach((route) => {
      const key = `${route.workload}:${route.logical_model_id}:${route.capability}`
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          workload: route.workload,
          logicalModel: route.logical_model_id,
          capability: route.capability,
          routes: [],
        })
      }
      groups.get(key).routes.push(route)
    })
    return [...groups.values()].sort((left, right) => {
      const leftDefault = left.workload === 'default' ? 1 : 0
      const rightDefault = right.workload === 'default' ? 1 : 0
      return leftDefault - rightDefault || left.logicalModel.localeCompare(right.logicalModel)
    })
  }, [routeView, routes])
  const keyHealth = (item) => {
    if (!item.is_active) return 'DISABLED'
    return keyHealthMap[item.id] || 'UNCONFIGURED'
  }

  const refresh = async () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ['platform', 'api-keys'] }),
    queryClient.invalidateQueries({ queryKey: ['platform', 'ai-routing-overview'] }),
    queryClient.invalidateQueries({ queryKey: ['platform', 'ai-catalog-models'] }),
    queryClient.invalidateQueries({ queryKey: ['platform', 'ai-route-page'] }),
    queryClient.invalidateQueries({ queryKey: ['platform', 'ai-provider-models'] }),
  ])

  const action = useMutation({
    mutationFn: async ({ fn, success }) => ({ data: await fn(), success }),
    onSuccess: async ({ success }) => { setMessage({ type: 'success', text: success }); await refresh() },
    onError: (error) => setMessage({ type: 'error', text: error?.response?.data?.detail || error?.message || '操作失败' }),
  })

  const openCreate = () => { setEditing(null); setForm(defaultForm(providers[0]?.id)); setMessage(null); setModalError(''); setModalOpen(true) }
  const openEdit = (item) => { setEditing(item); setForm({ name: item.name, api_key: '', provider_key: item.provider_key, is_default: !!item.is_default }); setMessage(null); setModalError(''); setModalOpen(true) }
  const submit = (event) => {
    event.preventDefault()
    setModalError('')
    if (!form.name.trim() || (!editing && !form.api_key.trim())) { setModalError('请填写名称和 API Key'); return }
    const payload = { name: form.name.trim(), provider_key: form.provider_key, is_default: !!form.is_default }
    if (form.api_key.trim()) payload.api_key = form.api_key.trim()
    action.mutate({
      fn: () => editing ? apiKeyApi.updateKey(editing.id, payload) : apiKeyApi.createKey(payload),
      success: editing ? 'API Key 已更新' : 'API Key 已保存，模型发现任务已触发',
    }, {
      onSuccess: () => setModalOpen(false),
      onError: (error) => setModalError(error?.response?.data?.detail || error?.message || '保存失败'),
    })
  }

  const toggleKey = (item) => {
    if (item.is_active && !window.confirm(`确定停用“${item.name}”吗？停用后不会再分配新请求。`)) return
    action.mutate({
      fn: () => item.is_active ? apiKeyApi.deactivateKey(item.id) : apiKeyApi.updateKey(item.id, { is_active: true }),
      success: `${item.name} 已${item.is_active ? '停用' : '启用'}`,
    })
  }

  const toggleRoute = (route) => {
    if (route.is_enabled && !window.confirm(`确定停用 ${route.logical_model_id} 的这条路由吗？`)) return
    action.mutate({
      fn: () => apiKeyApi.updateRoute(route.id, { is_enabled: !route.is_enabled }),
      success: `路由已${route.is_enabled ? '停用' : '启用'}`,
    })
  }

  const toggleVideoProvider = (item) => {
    if (item.is_enabled && !window.confirm(`确定全局停用 ${item.provider_label} 的 ${item.model_label} 吗？`)) return
    action.mutate({
      fn: () => apiKeyApi.updateProviderModel(item.provider_key, item.model_id, { is_enabled: !item.is_enabled }),
      success: `${item.provider_label} ${item.model_label} 已${item.is_enabled ? '停用' : '启用'}`,
    })
  }

  const busy = keysQuery.isLoading || providersQuery.isLoading || overviewQuery.isLoading
  const fatal = keysQuery.error || providersQuery.error || overviewQuery.error || catalogModelsQuery.error || routesQuery.error

  return (
    <div className="ai-admin">
      <header className="page-header">
        <div>
          <h1 className="page-title">AI 供应商与路由中心</h1>
          <div className="small-muted">自动发现模型，按业务设置调用优先级；服务商异常时熔断并切换健康路由。</div>
        </div>
        <div className="page-header__extra ai-admin__actions">
          <button type="button" className="btn" disabled={action.isPending} onClick={() => action.mutate({ fn: apiKeyApi.discoverAll, success: '全部供应商模型发现已完成' })}>刷新模型目录</button>
          <button type="button" className="btn btn--primary" onClick={openCreate}>新增 API Key</button>
        </div>
      </header>

      <section className="ai-summary">
        {[
          ['已接入供应商', summary.providers ?? providers.length, `${summary.active_keys || 0} 个启用 Key`],
          ['已发现模型', summary.discovered_models || 0, `${summary.available_models || 0} 个上游可用`],
          ['可调用路由', summary.eligible_routes || 0, `共 ${summary.routes || 0} 条策略`],
          ['故障熔断', summary.open_circuits || 0, summary.open_circuits ? '已自动停止分配' : '当前无熔断'],
        ].map(([label, value, hint]) => <div className="ai-summary__item" key={label}><span>{label}</span><strong>{value}</strong><small>{hint}</small></div>)}
      </section>

      <nav className="ai-tabs" aria-label="AI 管理页面">
        {TABS.map(([id, label]) => <button type="button" key={id} className={tab === id ? 'is-active' : ''} onClick={() => setTab(id)}>{label}</button>)}
      </nav>

      {message && <div className={`alert alert--${message.type === 'error' ? 'error' : 'success'} ai-admin__message`}>{typeof message.text === 'string' ? message.text : JSON.stringify(message.text)}</div>}
      {fatal && <div className="alert alert--error ai-admin__message">{fatal?.response?.data?.detail || fatal?.message || '数据加载失败'}</div>}
      {busy ? <Loading /> : null}

      {!busy && tab === 'providers' && <>
        <section className="ai-provider-grid">
          {(overview.providers || providers).map((provider) => <article className="card ai-provider" key={provider.id}>
            <div className="ai-provider__head"><div><h3>{provider.label}</h3><span>{provider.id}</span></div><Status ok={(provider.healthy_route_count || 0) > 0} value={(provider.healthy_route_count || 0) > 0 ? 'HEALTHY' : 'UNKNOWN'} /></div>
            <div className="ai-provider__metrics"><div><strong>{provider.active_key_count || 0}</strong><span>启用 Key</span></div><div><strong>{provider.available_model_count || 0}</strong><span>可用模型</span></div><div><strong>{provider.healthy_route_count || 0}</strong><span>健康路由</span></div></div>
            <div className="small-muted">能力：{(provider.capabilities || []).join(' / ') || '—'} · 最近发现：{fmtTime(provider.last_discovered_at)}</div>
          </article>)}
        </section>
        <section className="card ai-section">
          <div className="card__header">API Key 凭据 <span className="small-muted">（只显示元数据，密钥不会回显）</span></div>
          <div className="card__body table-wrapper"><table className="table"><thead><tr><th>名称</th><th>供应商</th><th>能力</th><th>默认优先级</th><th>状态</th><th>操作</th></tr></thead><tbody>
            {keys.map((item) => { const health = keyHealth(item); return <tr key={item.id}><td><strong>{item.name}</strong>{item.is_default && <span className="ai-tag">默认</span>}</td><td>{item.provider_label || providerMap[item.provider_key] || item.provider_key}</td><td>{(item.capabilities || []).join(' / ') || '—'}</td><td>{Object.entries(item.model_priorities || {}).map(([model, priority]) => `${model}: ${priority}`).join('；') || '按路由策略'}</td><td><Status value={health} ok={health === 'HEALTHY'} /><div className="small-muted">{item.is_active ? '凭据已启用' : '凭据已停用'}</div></td><td className="ai-nowrap"><button className="btn btn--sm" type="button" onClick={() => openEdit(item)}>编辑</button>{item.supports_model_discovery && <button className="btn btn--sm" type="button" disabled={action.isPending || !item.is_active} onClick={() => action.mutate({ fn: () => apiKeyApi.discoverKey(item.id), success: `${item.name} 模型发现完成` })}>发现模型</button>}<button className={`btn btn--sm${item.is_active ? ' btn--danger' : ''}`} type="button" disabled={action.isPending} onClick={() => toggleKey(item)}>{item.is_active ? '停用' : '启用'}</button></td></tr> })}
          </tbody></table></div>
        </section>
      </>}

      {!busy && tab === 'models' && <section className="card ai-section">
        <div className="card__header ai-filter"><span>上游模型目录</span><div><input className="input" placeholder="搜索供应商或模型" value={modelSearch} onChange={(e) => { setModelSearch(e.target.value); setModelPage(1) }} /><select className="input" value={capability} onChange={(e) => { setCapability(e.target.value); setModelPage(1) }}><option value="">全部能力</option><option value="text">文本</option><option value="multimodal">多模态</option><option value="image">图像</option><option value="video">视频</option><option value="audio">音频</option></select></div></div>
        <div className="card__body table-wrapper"><table className="table"><thead><tr><th>供应商</th><th>模型 ID</th><th>推断能力</th><th>接口模式</th><th>发现状态</th><th>最后发现</th></tr></thead><tbody>
          {catalogModels.map((item) => <tr key={item.id}><td>{providerMap[item.provider_key] || item.provider_key}</td><td><strong>{item.provider_model_id}</strong></td><td>{(item.capabilities || []).map((value) => <span className="ai-tag" key={value}>{value}</span>)}</td><td>{(item.endpoint_modes || []).join(' / ')}</td><td><Status value={item.lifecycle_status} ok={item.is_available} /></td><td>{fmtTime(item.last_seen_at)}</td></tr>)}
          {!catalogModelsQuery.isLoading && !catalogModels.length && <tr><td colSpan="6" className="empty">没有符合条件的模型</td></tr>}
        </tbody></table></div>
        {catalogModelsQuery.isLoading ? <Loading /> : <Pager page={catalogModelsQuery.data?.page || modelPage} pageSize={catalogModelsQuery.data?.page_size || PAGE_SIZE} total={catalogModelsQuery.data?.total || 0} onPage={setModelPage} />}
      </section>}

      {!busy && tab === 'routes' && <>
        <section className="card ai-section"><div className="card__header ai-filter"><span>统一路由策略 <span className="small-muted">按业务场景和逻辑模型分组，只有同组路由才比较优先级</span></span><div><input className="input" placeholder="搜索逻辑模型、供应商或 Key" value={routeSearch} onChange={(e) => { setRouteSearch(e.target.value); setRoutePage(1) }} /></div></div>
          <div className="ai-route-viewbar" role="group" aria-label="路由显示范围">
            {[
              ['enabled', '正在使用', enabledRouteCount],
              ['candidates', '候选路由', candidateRouteCount],
              ['all', '全部', summary.routes || 0],
            ].map(([value, label, count]) => <button type="button" key={value} aria-pressed={routeView === value} className={routeView === value ? 'is-active' : ''} onClick={() => { setRouteView(value); setRoutePage(1) }}><span>{label}</span><strong>{count}</strong></button>)}
          </div>
          {routeView === 'enabled' && <div className="ai-route-note"><strong>当前实际调用配置</strong><span>同一分组有多条路由时才形成主备链；只有一条时显示为单一路由。</span></div>}
          {routeView === 'candidates' && <div className="ai-route-note ai-route-note--muted"><strong>候选模型池</strong><span>这些路由不会接收请求。请先探测，确认成功后再启用。</span></div>}
          <div className="card__body table-wrapper"><table className="table"><thead><tr><th>调用角色</th><th>业务 / 逻辑模型</th><th>服务商模型与 Key</th><th>能力</th><th>优先级</th><th>健康</th><th>状态</th><th>操作</th></tr></thead><tbody>
          {routeGroups.map((group) => <Fragment key={group.key}>
            {routeView === 'enabled' && <tr className="ai-route-group"><td colSpan="8"><div><strong>{WORKLOAD_LABELS[group.workload] || group.workload}</strong><span className="ai-route-group__model">{group.logicalModel}</span><span className="ai-tag">{CAPABILITY_LABELS[group.capability] || group.capability}</span><span className="small-muted">{group.routes.length > 1 ? `${group.routes.length} 条主备路由` : '1 条独立路由'}</span></div></td></tr>}
            {group.routes.map((route) => <tr key={route.id} className={route.is_enabled ? 'ai-route-row--enabled' : ''}><td><RouteRole rank={routeRankMap[route.id]} enabled={route.is_enabled} groupSize={group.routes.length} /></td><td><strong>{route.logical_model_id}</strong><div className="small-muted">{WORKLOAD_LABELS[route.workload] || route.workload}</div></td><td>{providerMap[route.provider_key] || route.provider_key} / {route.provider_model_id}<div className="small-muted">{route.key_name}</div></td><td>{CAPABILITY_LABELS[route.capability] || route.capability}</td><td><input className="input ai-priority" aria-label={`${route.logical_model_id} ${providerMap[route.provider_key] || route.provider_key} 优先级`} type="number" min="1" max="9999" defaultValue={route.priority} onBlur={(e) => { const value = Number(e.target.value); if (value !== route.priority) action.mutate({ fn: () => apiKeyApi.updateRoute(route.id, { priority: value }), success: '路由优先级已更新' }) }} /><div className="small-muted">数字越小越优先</div></td><td><Status value={route.health_status} /><div className="small-muted">{route.latency_ema_ms ? `${route.latency_ema_ms} ms` : '未统计'}</div></td><td><Status ok={route.is_eligible} value={route.is_enabled ? (route.is_verified ? 'HEALTHY' : 'UNKNOWN') : 'DISABLED'} /></td><td className="ai-nowrap">{route.adapter_type === 'openai_chat_completions' && <button className="btn btn--sm" type="button" disabled={action.isPending} onClick={() => action.mutate({ fn: () => apiKeyApi.probeRoute(route.id, { enable_on_success: !route.is_enabled }), success: '路由探测成功' })}>探测</button>}<button className="btn btn--sm" type="button" disabled={action.isPending || (!route.is_verified && !route.is_enabled)} onClick={() => toggleRoute(route)}>{route.is_enabled ? '停用' : '启用'}</button>{route.health_status === 'CIRCUIT_OPEN' && <button className="btn btn--sm btn--danger" type="button" onClick={() => action.mutate({ fn: () => apiKeyApi.resetRouteCircuit(route.id), success: '熔断已重置，请重新探测' })}>重置熔断</button>}</td></tr>)}
          </Fragment>)}
          {!routesQuery.isLoading && !routes.length && <tr><td colSpan="8" className="empty">没有符合条件的路由</td></tr>}
        </tbody></table></div>{routesQuery.isLoading ? <Loading /> : <Pager page={routesQuery.data?.page || routePage} pageSize={routesQuery.data?.page_size || PAGE_SIZE} total={routesQuery.data?.total || 0} onPage={setRoutePage} />}</section>
        {!!providerModelsQuery.data?.length && <section className="card ai-section"><div className="card__header">视频供应商全局开关</div><div className="card__body table-wrapper"><table className="table"><thead><tr><th>供应商</th><th>视频模型</th><th>输入上限</th><th>全局状态</th></tr></thead><tbody>{providerModelsQuery.data.map((item) => <tr key={`${item.provider_key}:${item.model_id}`}><td>{item.provider_label}</td><td>{item.model_label}</td><td>最多 {item.reference_image_limit} 张参考图</td><td><button type="button" className={`btn btn--sm${item.is_enabled ? '' : ' btn--danger'}`} onClick={() => toggleVideoProvider(item)}>{item.is_enabled ? '已启用' : '已停用'}</button></td></tr>)}</tbody></table></div></section>}
      </>}

      {!busy && tab === 'health' && <section className="card ai-section"><div className="card__header">最近 50 次路由尝试 <span className="small-muted">仅记录路由元数据，不保存提示词、图像或密钥</span></div><div className="card__body table-wrapper"><table className="table"><thead><tr><th>时间</th><th>请求 ID</th><th>逻辑模型</th><th>实际服务商 / 模型</th><th>结果</th><th>耗时 / 错误</th></tr></thead><tbody>{(overview.recent_attempts || []).map((item) => <tr key={item.id}><td>{fmtTime(item.created_at)}</td><td className="ai-mono">{item.request_id}</td><td>{item.logical_model_id}</td><td>{providerMap[item.provider_key] || item.provider_key} / {item.provider_model_id}</td><td><Status value={item.status} /></td><td>{item.latency_ms != null ? `${item.latency_ms} ms` : '—'}{item.error_class && <div className="small-muted">{item.error_class} / HTTP {item.upstream_status_code || '—'}</div>}</td></tr>)}{!overview.recent_attempts?.length && <tr><td colSpan="6" className="empty">暂无调用记录</td></tr>}</tbody></table></div></section>}

      <Modal open={modalOpen} title={editing ? '编辑 API Key' : '新增 API Key'} onClose={() => !action.isPending && setModalOpen(false)}>
        <form onSubmit={submit} className="ai-key-form"><label><span>名称</span><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label><label><span>供应商</span><select className="input" value={form.provider_key} disabled={!!editing} onChange={(e) => setForm({ ...defaultForm(e.target.value), name: form.name, api_key: form.api_key })}>{providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><label><span>API Key {editing ? '（留空不修改）' : ''}</span><input className="input" type="password" autoComplete="new-password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} /></label><div className="small-muted">保存后由后端加密存储。支持发现的供应商会自动同步模型目录；新发现路由默认关闭，必须探测成功后才能启用。</div>{modalError && <div className="alert alert--error">{modalError}</div>}<label className="ai-checkbox"><input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />设为该供应商默认 Key</label><div className="ai-modal-actions"><button type="button" className="btn" onClick={() => setModalOpen(false)}>取消</button><button type="submit" className="btn btn--primary" disabled={action.isPending}>{action.isPending ? '保存中...' : '保存'}</button></div></form>
      </Modal>
    </div>
  )
}
