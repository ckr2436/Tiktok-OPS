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
} from '../service.js'

const PAGE_SIZE = 20
const DOMAIN_PATTERN = /^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/

const MODE_OPTIONS = [
  { value: '', label: '全部模式' },
  { value: 'whitelist', label: '白名单' },
  { value: 'blacklist', label: '黑名单' },
]

const ENABLED_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'enabled', label: '仅启用' },
  { value: 'disabled', label: '仅停用' },
]

export default function PlatformPolicies() {
  const [searchParams, setSearchParams] = useSearchParams()

  const [providerKey, setProviderKey] = useState(() => searchParams.get('provider_key') || '')
  const [mode, setMode] = useState(() => searchParams.get('mode') || '')
  const [domainFilter, setDomainFilter] = useState(() => searchParams.get('domain') || '')
  const [enabledFilter, setEnabledFilter] = useState(() => searchParams.get('enabled') || '')
  const [page, setPage] = useState(() => {
    const p = parseInt(searchParams.get('page') || '1', 10)
    return Number.isFinite(p) && p > 0 ? p : 1
  })

  const [providers, setProviders] = useState([])
  const providerLabel = useMemo(() => {
    const map = new Map()
    providers.forEach(item => map.set(item.key, item.name))
    return map
  }, [providers])

  const [policies, setPolicies] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [refreshToken, setRefreshToken] = useState(0)

  const [modalState, setModalState] = useState({ open: false, mode: 'create', policy: null })
  const [togglePending, setTogglePending] = useState(new Set())
  const [toast, setToast] = useState(null)

  const showToast = useCallback((message, tone = 'info') => {
    setToast({ id: Date.now(), message, tone })
  }, [])

  useEffect(() => {
    let cancelled = false
    async function run() {
      try {
        const data = await listPolicyProviders()
        if (!cancelled) {
          setProviders(Array.isArray(data) ? data : [])
        }
      } catch (err) {
        if (!cancelled) {
          showToast(err?.message || '加载平台提供方失败', 'error')
        }
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [showToast])

  const updateSearchParams = useCallback((next) => {
    const params = new URLSearchParams()
    if (next.provider_key) params.set('provider_key', next.provider_key)
    if (next.mode) params.set('mode', next.mode)
    if (next.domain?.trim()) params.set('domain', next.domain.trim())
    if (next.enabled) params.set('enabled', next.enabled)
    params.set('page', String(next.page))
    setSearchParams(params, { replace: true })
  }, [setSearchParams])

  useEffect(() => {
    const nextProvider = searchParams.get('provider_key') || ''
    const nextMode = searchParams.get('mode') || ''
    const nextDomain = searchParams.get('domain') || ''
    const nextEnabled = searchParams.get('enabled') || ''
    const nextPageRaw = parseInt(searchParams.get('page') || '1', 10)
    const nextPage = Number.isFinite(nextPageRaw) && nextPageRaw > 0 ? nextPageRaw : 1

    if (nextProvider !== providerKey) setProviderKey(nextProvider)
    if (nextMode !== mode) setMode(nextMode)
    if (nextDomain !== domainFilter) setDomainFilter(nextDomain)
    if (nextEnabled !== enabledFilter) setEnabledFilter(nextEnabled)
    if (nextPage !== page) setPage(nextPage)
  }, [searchParams, providerKey, mode, domainFilter, enabledFilter, page])

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      setError('')
      try {
        const query = {
          provider_key: providerKey || undefined,
          mode: mode || undefined,
          domain: domainFilter.trim() || undefined,
          enabled: enabledFilter || undefined,
          page,
          page_size: PAGE_SIZE,
        }
        const data = await listPolicies(query)
        if (cancelled) return
        const items = Array.isArray(data?.items) ? data.items : []
        setPolicies(items)
        setTotal(Number.isFinite(data?.total) ? data.total : 0)
        const pageSize = Number.isFinite(data?.page_size) ? data.page_size : PAGE_SIZE
        const totalPages = Math.max(1, Math.ceil((data?.total || 0) / pageSize))
        if (page > totalPages) {
          setPage(totalPages)
          updateSearchParams({
            provider_key: providerKey,
            mode,
            domain: domainFilter,
            enabled: enabledFilter,
            page: totalPages,
          })
        }
      } catch (err) {
        if (!cancelled) {
          setPolicies([])
          setTotal(0)
          setError(err?.message || '加载策略失败')
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [providerKey, mode, domainFilter, enabledFilter, page, refreshToken, updateSearchParams])

  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(null), toast.duration || 3200)
    return () => clearTimeout(timer)
  }, [toast])

  const totalPages = useMemo(() => {
    return Math.max(1, Math.ceil((total || 0) / PAGE_SIZE))
  }, [total])

  const handleFilterChange = useCallback(
    (key, value) => {
      const next = {
        provider_key: providerKey,
        mode,
        domain: domainFilter,
        enabled: enabledFilter,
        page,
      }
      if (key === 'provider_key') {
        next.provider_key = value
        setProviderKey(value)
      }
      if (key === 'mode') {
        next.mode = value
        setMode(value)
      }
      if (key === 'domain') {
        next.domain = value
        setDomainFilter(value)
      }
      if (key === 'enabled') {
        next.enabled = value
        setEnabledFilter(value)
      }
      if (key !== 'page') {
        next.page = 1
        setPage(1)
      }
      updateSearchParams(next)
    },
    [providerKey, mode, domainFilter, enabledFilter, page, updateSearchParams]
  )

  const handlePageChange = useCallback(
    (nextPage) => {
      const target = Math.min(Math.max(1, nextPage), totalPages)
      setPage(target)
      updateSearchParams({
        provider_key: providerKey,
        mode,
        domain: domainFilter,
        enabled: enabledFilter,
        page: target,
      })
    },
    [providerKey, mode, domainFilter, enabledFilter, totalPages, updateSearchParams]
  )

  const openCreateModal = () => {
    setModalState({ open: true, mode: 'create', policy: null })
  }

  const openEditModal = (policy) => {
    setModalState({ open: true, mode: 'edit', policy })
  }

  const closeModal = () => {
    setModalState({ open: false, mode: 'create', policy: null })
  }

  const refresh = useCallback(() => {
    setRefreshToken((x) => x + 1)
  }, [])

  const handleModalSuccess = useCallback(
    (message) => {
      closeModal()
      showToast(message, 'success')
      refresh()
    },
    [refresh, showToast]
  )

  const handleToggle = async (policy) => {
    const nextState = !policy.is_enabled
    setPolicies((prev) => prev.map((item) => (item.id === policy.id ? { ...item, is_enabled: nextState } : item)))
    setTogglePending((prev) => new Set([...prev, policy.id]))
    try {
      await togglePolicy(policy.id, nextState)
      showToast(`策略已${nextState ? '启用' : '停用'}`, 'success')
      refresh()
    } catch (err) {
      setPolicies((prev) => prev.map((item) => (item.id === policy.id ? { ...item, is_enabled: policy.is_enabled } : item)))
      showToast(err?.message || '更新失败', 'error')
    } finally {
      setTogglePending((prev) => {
        const next = new Set(prev)
        next.delete(policy.id)
        return next
      })
    }
  }

  const handleDelete = async (policy) => {
    if (!confirm(`确定删除策略「${policy.domain}」吗？`)) return
    try {
      await deletePolicy(policy.id)
      showToast('策略已删除', 'success')
      refresh()
    } catch (err) {
      showToast(err?.message || '删除失败', 'error')
    }
  }

  return (
    <div className="card card--elevated" style={{ padding: 16 }}>
      <header style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
        <div>
          <h3 style={{ margin: 0 }}>平台域名策略</h3>
          <p className="small-muted" style={{ margin: '4px 0 0' }}>
            管理各平台的白名单 / 黑名单域名，配置更改即时生效并记录审计日志。
          </p>
        </div>
        <button className="btn" onClick={openCreateModal}>
          新建策略
        </button>
      </header>

      <section
        aria-label="筛选条件"
        style={{
          display: 'flex',
          gap: 12,
          flexWrap: 'wrap',
          marginBottom: 16,
          alignItems: 'center',
        }}
      >
        <label className="input-group">
          <span className="input-group__label">提供方</span>
          <select
            className="input"
            value={providerKey}
            onChange={(e) => handleFilterChange('provider_key', e.target.value)}
          >
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
          <select className="input" value={mode} onChange={(e) => handleFilterChange('mode', e.target.value)}>
            {MODE_OPTIONS.map((opt) => (
              <option key={opt.value || 'all'} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>

        <label className="input-group" style={{ minWidth: 220, flex: '1 1 220px' }}>
          <span className="input-group__label">域名包含</span>
          <input
            className="input"
            placeholder="输入域名关键字"
            value={domainFilter}
            onChange={(e) => handleFilterChange('domain', e.target.value)}
          />
        </label>

        <label className="input-group">
          <span className="input-group__label">状态</span>
          <select
            className="input"
            value={enabledFilter}
            onChange={(e) => handleFilterChange('enabled', e.target.value)}
          >
            {ENABLED_OPTIONS.map((opt) => (
              <option key={opt.value || 'all'} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
      </section>

      {error && (
        <div className="alert alert--error" role="alert" style={{ marginBottom: 12 }}>
          {error}
          <button className="btn ghost" style={{ marginLeft: 12 }} onClick={() => refresh()}>
            重试
          </button>
        </div>
      )}

      <div className="table-wrap" style={{ border: '1px solid var(--border)', borderRadius: 12, overflowX: 'auto' }}>
        <table style={{ width: '100%', minWidth: 960, borderCollapse: 'collapse' }}>
          <thead style={{ background: 'var(--panel-2)' }}>
            <tr>
              <Th>提供方</Th>
              <Th>模式</Th>
              <Th>域名</Th>
              <Th>状态</Th>
              <Th>描述</Th>
              <Th>最近更新</Th>
              <Th align="right">操作</Th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={7} style={{ padding: 24 }}>
                  <Loading text="加载中..." />
                </td>
              </tr>
            ) : policies.length === 0 ? (
              <tr>
                <td colSpan={7} style={{ padding: 24, color: 'var(--muted)', textAlign: 'center' }}>
                  暂无策略，可通过右上角按钮创建。
                </td>
              </tr>
            ) : (
              policies.map((item) => (
                <tr key={item.id} style={{ borderTop: '1px solid var(--border)' }}>
                  <Td>{providerLabel.get(item.provider_key) || item.provider_key}</Td>
                  <Td>{item.mode === 'whitelist' ? '白名单' : '黑名单'}</Td>
                  <Td mono>{item.domain}</Td>
                  <Td>
                    <StatusBadge enabled={item.is_enabled} />
                  </Td>
                  <Td>{item.description || <span className="small-muted">未填写</span>}</Td>
                  <Td>{formatDate(item.updated_at)}</Td>
                  <Td>
                    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, flexWrap: 'wrap' }}>
                      <button className="btn ghost" onClick={() => openEditModal(item)}>
                        编辑
                      </button>
                      <button
                        className="btn ghost"
                        onClick={() => handleToggle(item)}
                        disabled={togglePending.has(item.id)}
                      >
                        {item.is_enabled ? '停用' : '启用'}
                      </button>
                      <button className="btn ghost" style={{ color: '#dc2626' }} onClick={() => handleDelete(item)}>
                        删除
                      </button>
                    </div>
                  </Td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <footer style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 16, flexWrap: 'wrap', gap: 12 }}>
        <div className="small-muted">共 {total} 条记录</div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <button className="btn ghost" disabled={page <= 1} onClick={() => handlePageChange(page - 1)}>
            上一页
          </button>
          <span className="small-muted">
            第 {page} / {totalPages} 页
          </span>
          <button className="btn ghost" disabled={page >= totalPages} onClick={() => handlePageChange(page + 1)}>
            下一页
          </button>
        </div>
      </footer>

      <PolicyModal
        open={modalState.open}
        mode={modalState.mode}
        policy={modalState.policy}
        providers={providers}
        onClose={closeModal}
        onSuccess={handleModalSuccess}
      />

      <Toast toast={toast} onDismiss={() => setToast(null)} />
    </div>
  )
}

function Th({ children, align = 'left' }) {
  return (
    <th
      style={{
        textAlign: align,
        padding: '10px 12px',
        fontWeight: 700,
        fontSize: 14,
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </th>
  )
}

function Td({ children, mono }) {
  return (
    <td
      style={{
        padding: '10px 12px',
        fontFamily: mono
          ? 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace'
          : undefined,
      }}
    >
      {children}
    </td>
  )
}

function StatusBadge({ enabled }) {
  const bg = enabled ? '#16a34a' : '#dc2626'
  const label = enabled ? '启用' : '停用'
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '4px 10px',
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 600,
        color: '#fff',
        background: bg,
      }}
    >
      <span
        aria-hidden="true"
        style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: '#fff' }}
      />
      {label}
    </span>
  )
}

function formatDate(value) {
  if (!value) return '—'
  try {
    return new Date(value).toLocaleString()
  } catch (e) {
    return value
  }
}

function Toast({ toast, onDismiss }) {
  if (!toast) return null
  const background = toast.tone === 'error' ? '#dc2626' : '#16a34a'
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: 'fixed',
        right: 24,
        bottom: 24,
        background,
        color: '#fff',
        padding: '12px 16px',
        borderRadius: 12,
        boxShadow: '0 10px 30px rgba(0,0,0,0.2)',
        display: 'flex',
        gap: 12,
        alignItems: 'center',
        zIndex: 2000,
        maxWidth: 360,
      }}
    >
      <span style={{ flex: 1 }}>{toast.message}</span>
      <button
        className="btn ghost"
        style={{ color: '#fff', borderColor: 'rgba(255,255,255,0.5)' }}
        onClick={onDismiss}
        aria-label="关闭通知"
      >
        ×
      </button>
    </div>
  )
}

function PolicyModal({ open, mode, policy, providers, onClose, onSuccess }) {
  const [providerKey, setProviderKey] = useState(policy?.provider_key || '')
  const [policyMode, setPolicyMode] = useState(policy?.mode || 'whitelist')
  const [domain, setDomain] = useState(policy?.domain || '')
  const [description, setDescription] = useState(policy?.description || '')
  const [enabled, setEnabled] = useState(policy?.is_enabled ?? true)
  const [errors, setErrors] = useState({})
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')

  useEffect(() => {
    if (!open) return
    setProviderKey(policy?.provider_key || '')
    setPolicyMode(policy?.mode || 'whitelist')
    setDomain(policy?.domain || '')
    setDescription(policy?.description || '')
    setEnabled(policy?.is_enabled ?? true)
    setErrors({})
    setSubmitError('')
  }, [open, policy])

  const title = mode === 'edit' ? '编辑策略' : '创建策略'

  const onSubmit = async (e) => {
    e.preventDefault()
    const nextErrors = {}
    if (!providerKey) nextErrors.provider_key = '请选择提供方'
    if (!policyMode) nextErrors.mode = '请选择模式'
    const normalizedDomain = domain.trim().toLowerCase()
    if (!normalizedDomain) {
      nextErrors.domain = '请输入域名'
    } else if (!DOMAIN_PATTERN.test(normalizedDomain)) {
      nextErrors.domain = '域名格式不正确，支持可选的 *. 前缀'
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) return

    setSubmitting(true)
    setSubmitError('')
    try {
      if (mode === 'edit' && policy) {
        await updatePolicy(policy.id, {
          mode: policyMode,
          domain: normalizedDomain,
          description: description.trim() || null,
          is_enabled: enabled,
        })
        onSuccess('策略已更新')
      } else {
        await createPolicy({
          provider_key: providerKey,
          mode: policyMode,
          domain: normalizedDomain,
          description: description.trim() || null,
          is_enabled: enabled,
        })
        onSuccess('策略已创建')
      }
    } catch (err) {
      const apiError = err?.payload?.error || {}
      const nextFieldErrors = {}
      if (apiError.code === 'PROVIDER_NOT_FOUND' || apiError.code === 'PROVIDER_NOT_CONFIGURED') {
        nextFieldErrors.provider_key = apiError.message || '所选提供方不可用'
      }
      if (apiError.code === 'PROVIDER_DISABLED') {
        nextFieldErrors.provider_key = apiError.message || '该提供方已停用'
      }
      if (apiError.code === 'INVALID_DOMAIN') {
        nextFieldErrors.domain = apiError.message || '域名格式不正确'
      }
      if (apiError.code === 'POLICY_EXISTS') {
        nextFieldErrors.domain = apiError.message || '该域名策略已存在'
      }

      if (Object.keys(nextFieldErrors).length > 0) {
        setErrors(nextFieldErrors)
        setSubmitError('')
      } else {
        setSubmitError(err?.message || '保存失败')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal open={open} title={title} onClose={onClose}>
      <form onSubmit={onSubmit} className="form" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <FormField label="提供方" error={errors.provider_key}>
          <select
            className="input"
            value={providerKey}
            onChange={(e) => setProviderKey(e.target.value)}
            disabled={mode === 'edit'}
          >
            <option value="">选择提供方</option>
            {providers.map((item) => (
              <option key={item.key} value={item.key}>
                {item.name}
              </option>
            ))}
          </select>
        </FormField>

        <FormField label="策略模式" error={errors.mode}>
          <select className="input" value={policyMode} onChange={(e) => setPolicyMode(e.target.value)}>
            <option value="whitelist">白名单</option>
            <option value="blacklist">黑名单</option>
          </select>
        </FormField>

        <FormField label="作用域名" error={errors.domain}>
          <input
            className="input"
            placeholder="例如：api.example.com 或 *.example.com"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            autoFocus
          />
        </FormField>

        <FormField label="描述">
          <textarea
            className="input"
            rows={3}
            placeholder="可选说明，帮助团队理解策略用途"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </FormField>

        <label className="checkbox">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span>启用此策略</span>
        </label>

        {submitError && (
          <div className="alert alert--error" role="alert">
            {submitError}
          </div>
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
          <button type="button" className="btn ghost" onClick={onClose}>
            取消
          </button>
          <button type="submit" className="btn" disabled={submitting}>
            {submitting ? '保存中…' : '保存'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
