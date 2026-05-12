import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useAppSelector } from '../../../../app/hooks.js'
import FormField from '../../../../components/ui/FormField.jsx'
import { fetchHermesCapabilities, postHermesAgent } from '../api.js'


function buildHermesPayload(form, fields, pageTitle) {
  const cleanedEntries = fields
    .map((field) => {
      const rawValue = form[field.name]
      const value = typeof rawValue === 'string' ? rawValue.trim() : rawValue
      return [field, value]
    })
    .filter(([, value]) => value !== '' && value != null)

  const inputJson = Object.fromEntries(cleanedEntries.map(([field, value]) => [field.name, value]))
  const input = cleanedEntries
    .map(([field, value]) => `${field.label || field.name}: ${value}`)
    .join('\n')

  return {
    title: pageTitle,
    input: input || null,
    input_json: Object.keys(inputJson).length ? inputJson : null,
  }
}

function getMissingRequiredFields(form, fields) {
  return fields.filter((field) => {
    if (!field.required) return false
    const value = form[field.name]
    if (typeof value === 'string') return value.trim() === ''
    return value == null || value === ''
  })
}

export default function HermesAgentPage({ title, description, endpoint, permissionKey, fields }) {
  const { wid } = useParams()
  const session = useAppSelector((s) => s.session.data)
  const sessionChecked = useAppSelector((s) => s.session.checked)
  const [form, setForm] = useState(() =>
    Object.fromEntries(fields.map((field) => [field.name, field.defaultValue || ''])),
  )
  const [loading, setLoading] = useState(false)
  const [requestError, setRequestError] = useState('')
  const [permissionError, setPermissionError] = useState('')
  const [result, setResult] = useState(null)
  const [permissionLoading, setPermissionLoading] = useState(true)
  const [hasPermission, setHasPermission] = useState(false)

  useEffect(() => {
    let mounted = true

    async function loadPermissions() {
      if (!sessionChecked || !wid) {
        setPermissionLoading(true)
        setHasPermission(false)
        setPermissionError('')
        return
      }

      setPermissionLoading(true)
      setHasPermission(false)
      setPermissionError('')

      try {
        const capabilities = await fetchHermesCapabilities(wid)
        const perms = session?.permissions || session?.perms || []
        const hasSessionPerms = Array.isArray(perms) && perms.length > 0
        const hasGeneralHermesPermission = hasSessionPerms && perms.includes('hermes_agent.use')
        const hasPageHermesPermission = hasSessionPerms && perms.includes(permissionKey)
        const requiresExplicitPermission = capabilities?.require_explicit_permission === true
        const allowedBySessionPermission = !requiresExplicitPermission || !hasSessionPerms || hasGeneralHermesPermission || hasPageHermesPermission
        const isFeatureEnabled = capabilities?.enabled !== false
        const role = String(session?.role || '').toLowerCase()
        const isTenantAdmin = role === 'owner' || role === 'admin'
        const memberAccessEnabled = capabilities?.allow_member !== false
        const allowed = isFeatureEnabled && (isTenantAdmin || (memberAccessEnabled && allowedBySessionPermission))

        if (mounted) setHasPermission(allowed)
      } catch (err) {
        console.error('load hermes permissions failed', err)
        if (mounted) {
          setHasPermission(false)
          setPermissionError(err?.message || '加载权限失败，请稍后重试。')
        }
      } finally {
        if (mounted) setPermissionLoading(false)
      }
    }

    loadPermissions()

    return () => {
      mounted = false
    }
  }, [wid, permissionKey, session, sessionChecked])

  const controlsDisabled = permissionLoading || !hasPermission || loading

  async function handleSubmit(event) {
    event.preventDefault()
    if (permissionLoading || !hasPermission) return

    const missingRequiredFields = getMissingRequiredFields(form, fields)
    if (missingRequiredFields.length > 0) {
      setRequestError(`请填写必填项：${missingRequiredFields.map((field) => field.label || field.name).join('、')}`)
      setResult(null)
      return
    }

    setLoading(true)
    setRequestError('')
    setResult(null)
    try {
      const payload = buildHermesPayload(form, fields, title)
      const response = await postHermesAgent(wid, endpoint, payload)
      setResult(response)
    } catch (err) {
      console.error(`call hermes-agent ${endpoint} failed`, err)
      setRequestError(err?.message || '请求失败，请稍后再试。')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="card" style={{ display: 'grid', gap: 16 }}>
      <div>
        <h2 style={{ margin: 0 }}>{title}</h2>
        <p className="muted" style={{ marginTop: 8 }}>{description}</p>
      </div>
      {permissionLoading ? <div className="muted">正在校验权限…</div> : null}
      {!permissionLoading && permissionError ? <div className="alert error">{permissionError}</div> : null}
      {!permissionLoading && !permissionError && !hasPermission ? <div className="alert error">您没有访问该 Hermes 功能的权限。</div> : null}
      <form onSubmit={handleSubmit} style={{ display: 'grid', gap: 12, opacity: controlsDisabled ? 0.6 : 1 }}>
        {fields.map((field) => (
          <FormField key={field.name} label={field.label} required={field.required}>
            <textarea
              className="input"
              rows={field.rows || 3}
              value={form[field.name] || ''}
              placeholder={field.placeholder || ''}
              required={Boolean(field.required)}
              onChange={(e) => setForm((prev) => ({ ...prev, [field.name]: e.target.value }))}
              disabled={controlsDisabled}
            />
          </FormField>
        ))}
        <div><button className="btn" type="submit" disabled={controlsDisabled}>{loading ? '生成中…' : permissionLoading ? '校验权限中…' : '开始生成'}</button></div>
      </form>
      {requestError ? <div className="alert error">{requestError}</div> : null}
      {result ? <div className="card" style={{ background: 'var(--panel-2)' }}><h3 style={{ marginTop: 0 }}>结果</h3><pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(result, null, 2)}</pre></div> : null}
    </div>
  )
}
