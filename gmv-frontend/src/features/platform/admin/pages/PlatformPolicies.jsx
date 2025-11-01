import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import FormField from '../../../../components/ui/FormField.jsx'
import Loading from '../../../../components/ui/Loading.jsx'
import Modal from '../../../../components/ui/Modal.jsx'
import {
  listPolicyProviders,
  listPolicies,
  createPolicy,
  updatePolicy,
  togglePolicy,
  deletePolicy,
  dryRunPolicy,
} from '../service.js'

const DOMAIN_PATTERN = /^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/
const MODE_VALUES = ['WHITELIST', 'BLACKLIST']
const ENFORCEMENT_VALUES = ['ENFORCE', 'DRYRUN', 'OFF']
const ALLOWED_SCOPE_KEYS = ['bc_ids', 'advertiser_ids', 'store_ids', 'product_ids']

const MODE_OPTIONS = [
  { value: '', label: '全部模式' },
  { value: 'WHITELIST', label: '白名单' },
  { value: 'BLACKLIST', label: '黑名单' },
]

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'enabled', label: '仅启用' },
  { value: 'disabled', label: '仅停用' },
]

function normalizeMode(value, fallback = MODE_VALUES[0]) {
  const normalized = (value ?? '').toString().trim().toUpperCase()
  if (MODE_VALUES.includes(normalized)) return normalized
  return fallback
}

function normalizeEnforcement(value, fallback = ENFORCEMENT_VALUES[0]) {
  const normalized = (value ?? '').toString().trim().toUpperCase()
  if (ENFORCEMENT_VALUES.includes(normalized)) return normalized
  return fallback
}

function normalizeStatus(value) {
  const normalized = (value ?? '').toString().trim().toLowerCase()
  if (['enabled', 'disabled'].includes(normalized)) return normalized
  return ''
}

function DomainEditor({ value, onChange, error }) {
  const [input, setInput] = useState('')

  const addDomain = useCallback(() => {
    const candidate = input.trim().toLowerCase()
    if (!candidate) return
    if (!DOMAIN_PATTERN.test(candidate)) return
    if (value.includes(candidate)) {
      setInput('')
      return
    }
    onChange([...value, candidate])
    setInput('')
  }, [input, onChange, value])

  const handleKeyDown = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      addDomain()
    }
  }

  const removeDomain = (domain) => {
    onChange(value.filter((item) => item !== domain))
  }

  return (
    <FormField label="域名列表" error={error} description="输入域名后回车，可选 *. 前缀。">
      <div className="chip-editor">
        <div className="chip-list">
          {value.map((domain) => (
            <span key={domain} className="chip">
              {domain}
              <button type="button" className="chip__remove" onClick={() => removeDomain(domain)}>
                ×
              </button>
            </span>
          ))}
        </div>
        <input
          className="input"
          placeholder="例如：api.example.com 或 *.example.com"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          onBlur={addDomain}
        />
      </div>
    </FormField>
  )
}

function useToast() {
  const [toast, setToast] = useState(null)

  const showToast = useCallback((message, tone = 'info', duration = 3000) => {
    setToast({ id: Date.now(), message, tone, duration })
  }, [])

  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(null), toast.duration)
    return () => clearTimeout(timer)
  }, [toast])

  return [toast, showToast]
}

function parseBusinessScopes(raw, setError) {
  if (!raw.trim()) return {}
  try {
    const parsed = JSON.parse(raw)
    const allowedTopKeys = ['include', 'exclude']
    const result = {}
    for (const topKey of allowedTopKeys) {
      if (!parsed[topKey]) continue
      if (typeof parsed[topKey] !== 'object') {
        setError(`${topKey} 必须是对象`)
        return null
      }
      result[topKey] = {}
      for (const key of Object.keys(parsed[topKey])) {
        if (!ALLOWED_SCOPE_KEYS.includes(key)) {
          setError(`不支持的业务范围键：${key}`)
          return null
        }
        const items = parsed[topKey][key]
        if (!Array.isArray(items)) {
          setError(`${topKey}.${key} 必须是字符串数组`)
          return null
        }
        const sanitized = Array.from(new Set(items.map((item) => item.trim()).filter(Boolean)))
        result[topKey][key] = sanitized
      }
    }
    return result
  } catch (err) {
    setError('业务范围 JSON 解析失败')
    return null
  }
}

function stringifyBusinessScopes(scopes) {
  const include = scopes?.include ?? {}
  const exclude = scopes?.exclude ?? {}
  if (!Object.keys(include).length && !Object.keys(exclude).length) return ''
  return JSON.stringify({ include, exclude }, null, 2)
}

function PolicyFormModal({ open, onClose, onSubmit, providers, initial }) {
  const [providerKey, setProviderKey] = useState(initial?.provider_key || '')
  const [name, setName] = useState(initial?.name || '')
  const [mode, setMode] = useState(() => normalizeMode(initial?.mode, MODE_VALUES[0]))
  const [enforcementMode, setEnforcementMode] = useState(() => normalizeEnforcement(initial?.enforcement_mode, ENFORCEMENT_VALUES[0]))
  const [domains, setDomains] = useState(() => initial?.domains?.slice?.() || [])
  const [businessScopes, setBusinessScopes] = useState(() => stringifyBusinessScopes(initial?.business_scopes || {}))
  const [description, setDescription] = useState(initial?.description || '')
  const [isEnabled, setIsEnabled] = useState(initial?.is_enabled ?? true)
  const [rateLimitRps, setRateLimitRps] = useState(initial?.limits?.rate_limit_rps ?? '')
  const [rateBurst, setRateBurst] = useState(initial?.limits?.rate_burst ?? '')
  const [cooldownSeconds, setCooldownSeconds] = useState(initial?.limits?.cooldown_seconds ?? 0)
  const [maxConcurrency, setMaxConcurrency] = useState(initial?.limits?.max_concurrency ?? '')
  const [maxEntities, setMaxEntities] = useState(initial?.limits?.max_entities_per_run ?? '')
  const [windowCron, setWindowCron] = useState(initial?.limits?.window_cron ?? '')
  const [errors, setErrors] = useState({})
  const [pending, setPending] = useState(false)

  useEffect(() => {
    if (!open) return
    setErrors({})
    setProviderKey(initial?.provider_key || '')
    setName(initial?.name || '')
    setMode(normalizeMode(initial?.mode, MODE_VALUES[0]))
    setEnforcementMode(normalizeEnforcement(initial?.enforcement_mode, ENFORCEMENT_VALUES[0]))
    setDomains(initial?.domains?.slice?.() || [])
    setBusinessScopes(stringifyBusinessScopes(initial?.business_scopes || {}))
    setDescription(initial?.description || '')
    setIsEnabled(initial?.is_enabled ?? true)
    setRateLimitRps(initial?.limits?.rate_limit_rps ?? '')
    setRateBurst(initial?.limits?.rate_burst ?? '')
    setCooldownSeconds(initial?.limits?.cooldown_seconds ?? 0)
    setMaxConcurrency(initial?.limits?.max_concurrency ?? '')
    setMaxEntities(initial?.limits?.max_entities_per_run ?? '')
    setWindowCron(initial?.limits?.window_cron ?? '')
  }, [open, initial])

  const providerOptions = useMemo(() => {
    return providers.map((item) => (
      <option key={item.key} value={item.key}>
        {item.name}
      </option>
    ))
  }, [providers])

  const submit = async (event) => {
    event.preventDefault()
    const nextErrors = {}
    if (!providerKey) nextErrors.provider_key = '请选择提供方'
    if (!name.trim()) nextErrors.name = '请输入策略名称'
    if (!domains.length) nextErrors.domains = '至少添加 1 个域名'
    const scopes = parseBusinessScopes(businessScopes, (message) => {
      nextErrors.business_scopes = message
    })
    if (scopes === null) {
      setErrors(nextErrors)
      return
    }

    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors)
      return
    }

    const payload = {
      provider_key: providerKey,
      name: name.trim(),
      mode,
      enforcement_mode: enforcementMode,
      domains,
      business_scopes: scopes,
      description: description.trim(),
      is_enabled: Boolean(isEnabled),
      rate_limit_rps: rateLimitRps === '' ? null : Number(rateLimitRps),
      rate_burst: rateBurst === '' ? null : Number(rateBurst),
      cooldown_seconds: Number(cooldownSeconds || 0),
      max_concurrency: maxConcurrency === '' ? null : Number(maxConcurrency),
      max_entities_per_run: maxEntities === '' ? null : Number(maxEntities),
      window_cron: windowCron.trim() || null,
    }

    setErrors({})
    setPending(true)
    try {
      await onSubmit(payload)
      onClose()
    } catch (err) {
      const response = err?.response?.data
      if (response?.error?.data?.fields) {
        setErrors(response.error.data.fields)
      } else {
        setErrors({ form: err?.message || '保存失败' })
      }
    } finally {
      setPending(false)
    }
  }

  if (!open) return null

  return (
    <Modal open={open} onClose={onClose} title={initial ? '编辑策略' : '新建策略'}>
      <form onSubmit={submit} className="form-grid">
        <FormField label="提供方" error={errors.provider_key}>
          <select className="input" value={providerKey} onChange={(e) => setProviderKey(e.target.value)}>
            <option value="">请选择提供方</option>
            {providerOptions}
          </select>
        </FormField>

        <FormField label="策略名称" error={errors.name}>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={128} />
        </FormField>

        <FormField label="策略模式">
          <select className="input" value={mode} onChange={(e) => setMode(normalizeMode(e.target.value))}>
            {MODE_VALUES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </FormField>

        <FormField label="执行模式">
          <select className="input" value={enforcementMode} onChange={(e) => setEnforcementMode(normalizeEnforcement(e.target.value))}>
            {ENFORCEMENT_VALUES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </FormField>

        <DomainEditor value={domains} onChange={setDomains} error={errors.domains} />

        <FormField
          label="业务范围 JSON"
          description={'可选，示例：{"include":{"bc_ids":["123"]}}'}
          error={errors.business_scopes}
        >
          <textarea
            className="input"
            value={businessScopes}
            onChange={(e) => setBusinessScopes(e.target.value)}
            rows={6}
          />
        </FormField>

        <FormField label="描述">
          <textarea className="input" value={description} onChange={(e) => setDescription(e.target.value)} rows={3} />
        </FormField>

        <FormField label="速率限制 (RPS)">
          <input
            className="input"
            type="number"
            min="1"
            value={rateLimitRps}
            onChange={(e) => setRateLimitRps(e.target.value)}
            placeholder="留空表示不限制"
          />
        </FormField>

        <FormField label="突发容量">
          <input
            className="input"
            type="number"
            min="1"
            value={rateBurst}
            onChange={(e) => setRateBurst(e.target.value)}
            placeholder="留空表示不限制"
          />
        </FormField>

        <FormField label="冷却秒数">
          <input
            className="input"
            type="number"
            min="0"
            value={cooldownSeconds}
            onChange={(e) => setCooldownSeconds(e.target.value)}
          />
        </FormField>

        <FormField label="最大并发">
          <input
            className="input"
            type="number"
            min="1"
            value={maxConcurrency}
            onChange={(e) => setMaxConcurrency(e.target.value)}
            placeholder="留空表示不限制"
          />
        </FormField>

        <FormField label="单次最大实体数">
          <input
            className="input"
            type="number"
            min="1"
            value={maxEntities}
            onChange={(e) => setMaxEntities(e.target.value)}
            placeholder="留空表示不限制"
          />
        </FormField>

        <FormField label="Cron 窗口">
          <input
            className="input"
            value={windowCron}
            onChange={(e) => setWindowCron(e.target.value)}
            placeholder="可选，例如：0 * * * *"
          />
        </FormField>

        <FormField label="启用状态">
          <label className="checkbox">
            <input type="checkbox" checked={isEnabled} onChange={(e) => setIsEnabled(e.target.checked)} />
            <span>启用此策略</span>
          </label>
        </FormField>

        {errors.form && <div className="alert alert--error">{errors.form}</div>}

        <footer className="modal__footer">
          <button type="button" className="btn ghost" onClick={onClose} disabled={pending}>
            取消
          </button>
          <button type="submit" className="btn" disabled={pending}>
            {pending ? '保存中…' : '保存'}
          </button>
        </footer>
      </form>
    </Modal>
  )
}

function DryRunModal({ open, onClose, onSubmit, policy }) {
  const [domain, setDomain] = useState(policy?.domains?.[0] ?? '')
  const [payload, setPayload] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)

  useEffect(() => {
    if (!open) return
    setDomain(policy?.domains?.[0] ?? '')
    setPayload('')
    setResult(null)
    setError('')
  }, [open, policy])

  if (!open) return null

  const run = async (event) => {
    event.preventDefault()
    setError('')
    let candidates = []
    if (payload.trim()) {
      try {
        const parsed = JSON.parse(payload)
        if (!Array.isArray(parsed)) throw new Error('需要数组')
        candidates = parsed
      } catch (err) {
        setError('候选 JSON 解析失败，应为数组结构')
        return
      }
    }
    if (!candidates.length && domain.trim()) {
      candidates = [{ domain }]
    }
    setPending(true)
    try {
      const body = await onSubmit({
        candidates,
      })
      setResult(body)
    } catch (err) {
      setError(err?.message || '测试失败')
    } finally {
      setPending(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={`测试策略 #${policy?.id || ''}`}>
      <form onSubmit={run} className="form-grid">
        <FormField label="测试域名" description="默认取策略第一个域名">
          <input className="input" value={domain} onChange={(e) => setDomain(e.target.value)} />
        </FormField>
        <FormField
          label="候选 JSON"
          description={'可选，数组形式，例如：[{"store_id":"s1"}]'}
        >
          <textarea className="input" rows={4} value={payload} onChange={(e) => setPayload(e.target.value)} />
        </FormField>
        {error && <div className="alert alert--error">{error}</div>}
        <footer className="modal__footer">
          <button type="button" className="btn ghost" onClick={onClose} disabled={pending}>
            关闭
          </button>
          <button type="submit" className="btn" disabled={pending}>
            {pending ? '测试中…' : '执行测试'}
          </button>
        </footer>
      </form>
      {result && (
        <section className="card" style={{ marginTop: 16 }}>
          <h4>测试结果</h4>
          <pre className="code-block" style={{ maxHeight: 320, overflow: 'auto' }}>
            {JSON.stringify(result, null, 2)}
          </pre>
        </section>
      )}
    </Modal>
  )
}

export default function PlatformPolicies() {
  const [searchParams, setSearchParams] = useSearchParams()

  const [providerKey, setProviderKey] = useState(() => searchParams.get('provider_key') || '')
  const [mode, setMode] = useState(() => normalizeMode(searchParams.get('mode'), ''))
  const [domainFilter, setDomainFilter] = useState(() => searchParams.get('domain') || '')
  const [statusFilter, setStatusFilter] = useState(() => normalizeStatus(searchParams.get('status')))
  const [page, setPage] = useState(() => {
    const raw = parseInt(searchParams.get('page') || '1', 10)
    return Number.isFinite(raw) && raw > 0 ? raw : 1
  })

  const [providers, setProviders] = useState([])
  const [policies, setPolicies] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [modalState, setModalState] = useState({ open: false, policy: null })
  const [dryRunState, setDryRunState] = useState({ open: false, policy: null })
  const [toast, showToast] = useToast()
  const [refreshToken, setRefreshToken] = useState(0)

  useEffect(() => {
    let cancelled = false
    async function loadProviders() {
      try {
        const data = await listPolicyProviders()
        if (!cancelled && Array.isArray(data)) {
          setProviders(data)
        }
      } catch (err) {
        if (!cancelled) showToast(err?.message || '加载提供方失败', 'error')
      }
    }
    loadProviders()
    return () => {
      cancelled = true
    }
  }, [showToast])

  const loadPolicies = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const query = {
        provider_key: providerKey || undefined,
        mode: mode || undefined,
        domain: domainFilter || undefined,
        status: statusFilter || undefined,
        page,
        page_size: 20,
        sort: '-updated_at',
      }
      const data = await listPolicies(query)
      const items = Array.isArray(data?.items) ? data.items : []
      setPolicies(items)
      setTotal(Number.isFinite(data?.total) ? data.total : 0)
    } catch (err) {
      setPolicies([])
      setTotal(0)
      setError(err?.message || '加载策略失败')
    } finally {
      setLoading(false)
    }
  }, [providerKey, mode, domainFilter, statusFilter, page])

  useEffect(() => {
    loadPolicies()
  }, [loadPolicies, refreshToken])

  useEffect(() => {
    const params = new URLSearchParams()
    if (providerKey) params.set('provider_key', providerKey)
    if (mode) params.set('mode', mode)
    if (domainFilter) params.set('domain', domainFilter)
    if (statusFilter) params.set('status', statusFilter)
    params.set('page', String(page))
    setSearchParams(params, { replace: true })
  }, [providerKey, mode, domainFilter, statusFilter, page, setSearchParams])

  const totalPages = useMemo(() => Math.max(1, Math.ceil(total / 20)), [total])

  const openCreateModal = () => setModalState({ open: true, policy: null })
  const openEditModal = (policy) => setModalState({ open: true, policy })
  const closeModal = () => setModalState({ open: false, policy: null })

  const openDryRunModal = (policy) => setDryRunState({ open: true, policy })
  const closeDryRunModal = () => setDryRunState({ open: false, policy: null })

  const refresh = () => setRefreshToken((x) => x + 1)

  const handleSubmit = async (payload) => {
    if (modalState.policy) {
      await updatePolicy(modalState.policy.id, payload)
      showToast('策略已更新', 'success')
    } else {
      await createPolicy(payload)
      showToast('策略已创建', 'success')
    }
    refresh()
  }

  const handleToggle = async (policy) => {
    const next = !policy.is_enabled
    setPolicies((prev) => prev.map((item) => (item.id === policy.id ? { ...item, is_enabled: next } : item)))
    try {
      await togglePolicy(policy.id, next)
      showToast(`策略已${next ? '启用' : '停用'}`, 'success')
      refresh()
    } catch (err) {
      setPolicies((prev) => prev.map((item) => (item.id === policy.id ? { ...item, is_enabled: policy.is_enabled } : item)))
      showToast(err?.message || '切换失败', 'error')
    }
  }

  const handleDelete = async (policy) => {
    if (!window.confirm(`确定删除策略「${policy.name}」吗？`)) return
    try {
      await deletePolicy(policy.id)
      showToast('策略已删除', 'success')
      refresh()
    } catch (err) {
      showToast(err?.message || '删除失败', 'error')
    }
  }

  const handleDryRun = async (payload) => {
    const response = await dryRunPolicy(dryRunState.policy.id, payload)
    return response
  }

  return (
    <div className="card card--elevated" style={{ padding: 16 }}>
      <header className="page-header">
        <div>
          <h3>平台策略管理</h3>
          <p className="small-muted">配置白名单 / 黑名单、业务范围以及限流，所有操作将记录审计日志。</p>
        </div>
        <button className="btn" onClick={openCreateModal}>
          新建策略
        </button>
      </header>

      <section className="filters">
        <label className="input-group">
          <span className="input-group__label">提供方</span>
          <select className="input" value={providerKey} onChange={(e) => setProviderKey(e.target.value)}>
            <option value="">全部提供方</option>
            {providers.map((item) => (
              <option key={item.key} value={item.key}>
                {item.name}
              </option>
            ))}
          </select>
        </label>

        <label className="input-group">
          <span className="input-group__label">模式</span>
          <select className="input" value={mode} onChange={(e) => setMode(normalizeMode(e.target.value, ''))}>
            {MODE_OPTIONS.map((opt) => (
              <option key={opt.value || 'all'} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>

        <label className="input-group" style={{ minWidth: 220, flex: '1 1 220px' }}>
          <span className="input-group__label">域名包含</span>
          <input className="input" value={domainFilter} onChange={(e) => setDomainFilter(e.target.value)} placeholder="关键字" />
        </label>

        <label className="input-group">
          <span className="input-group__label">状态</span>
          <select className="input" value={statusFilter} onChange={(e) => setStatusFilter(normalizeStatus(e.target.value))}>
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value || 'all'} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
      </section>

      {error && (
        <div className="alert alert--error" role="alert">
          {error}
          <button className="btn ghost" onClick={loadPolicies} style={{ marginLeft: 12 }}>
            重试
          </button>
        </div>
      )}

      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>提供方</th>
              <th>模式</th>
              <th>域名数量</th>
              <th>执行模式</th>
              <th>状态</th>
              <th>限流</th>
              <th>最近更新</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={8} style={{ padding: 24 }}>
                  <Loading text="加载中..." />
                </td>
              </tr>
            ) : policies.length === 0 ? (
              <tr>
                <td colSpan={8} className="empty">
                  暂无策略
                </td>
              </tr>
            ) : (
              policies.map((policy) => (
                <tr key={policy.id}>
                  <td>{policy.provider_key}</td>
                  <td>{policy.mode}</td>
                  <td>{policy.domains?.length ?? 0}</td>
                  <td>{policy.enforcement_mode}</td>
                  <td>{policy.is_enabled ? '启用' : '停用'}</td>
                  <td>
                    {policy.limits?.rate_limit_rps
                      ? `${policy.limits.rate_limit_rps} rps`
                      : '—'}
                    {policy.limits?.cooldown_seconds ? ` / 冷却 ${policy.limits.cooldown_seconds}s` : ''}
                  </td>
                  <td>{new Date(policy.updated_at).toLocaleString()}</td>
                  <td>
                    <div className="table-actions">
                      <button className="btn ghost" onClick={() => openEditModal(policy)}>
                        编辑
                      </button>
                      <button className="btn ghost" onClick={() => handleToggle(policy)}>
                        {policy.is_enabled ? '停用' : '启用'}
                      </button>
                      <button className="btn ghost" onClick={() => openDryRunModal(policy)}>
                        测试
                      </button>
                      <button className="btn danger ghost" onClick={() => handleDelete(policy)}>
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <footer className="pagination">
        <span>
          第 {page} / {totalPages} 页，共 {total} 条记录
        </span>
        <div className="pagination__controls">
          <button className="btn ghost" onClick={() => setPage(Math.max(1, page - 1))} disabled={page <= 1}>
            上一页
          </button>
          <button className="btn ghost" onClick={() => setPage(Math.min(totalPages, page + 1))} disabled={page >= totalPages}>
            下一页
          </button>
        </div>
      </footer>

      {toast && (
        <div className={`toast toast--${toast.tone}`} role="status">
          {toast.message}
        </div>
      )}

      <PolicyFormModal
        open={modalState.open}
        onClose={closeModal}
        onSubmit={handleSubmit}
        providers={providers}
        initial={modalState.policy}
      />

      <DryRunModal
        open={dryRunState.open}
        onClose={closeDryRunModal}
        onSubmit={handleDryRun}
        policy={dryRunState.policy}
      />
    </div>
  )
}
