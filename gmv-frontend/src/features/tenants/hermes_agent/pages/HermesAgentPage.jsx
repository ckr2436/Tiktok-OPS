import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import FormField from '../../../../components/ui/FormField.jsx'
import { fetchHermesPermissions, postHermesAgent } from '../api.js'

export default function HermesAgentPage({ title, description, endpoint, permissionKey, fields }) {
  const { wid } = useParams()
  const [form, setForm] = useState(() =>
    Object.fromEntries(fields.map((field) => [field.name, field.defaultValue || ''])),
  )
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [permissionLoading, setPermissionLoading] = useState(true)
  const [hasPermission, setHasPermission] = useState(true)

  useEffect(() => {
    let mounted = true
    async function loadPermissions() {
      setPermissionLoading(true)
      try {
        const perms = await fetchHermesPermissions(wid)
        const canUse = perms.includes('hermes_agent.use')
        const canVisitCurrent = perms.includes(permissionKey)
        if (mounted) setHasPermission(canUse && canVisitCurrent)
      } catch (err) {
        console.error('load hermes permissions failed', err)
        if (mounted) {
          setHasPermission(false)
          setError(err?.message || '加载权限失败，请稍后重试。')
        }
      } finally {
        if (mounted) setPermissionLoading(false)
      }
    }
    loadPermissions()
    return () => {
      mounted = false
    }
  }, [wid, permissionKey])

  async function handleSubmit(event) {
    event.preventDefault()
    setLoading(true)
    setError('')
    setResult(null)
    try {
      const payload = Object.fromEntries(
        Object.entries(form).map(([key, value]) => [key, typeof value === 'string' ? value.trim() : value]),
      )
      const response = await postHermesAgent(wid, endpoint, payload)
      setResult(response)
    } catch (err) {
      console.error(`call hermes-agent ${endpoint} failed`, err)
      setError(err?.message || '请求失败，请稍后再试。')
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
      {!permissionLoading && !hasPermission ? <div className="alert error">您没有访问该 Hermes 功能的权限。</div> : null}
      <form onSubmit={handleSubmit} style={{ display: 'grid', gap: 12, opacity: hasPermission ? 1 : 0.6 }}>
        {fields.map((field) => (
          <FormField key={field.name} label={field.label} required={field.required}>
            <textarea
              className="input"
              rows={field.rows || 3}
              value={form[field.name] || ''}
              placeholder={field.placeholder || ''}
              onChange={(e) => setForm((prev) => ({ ...prev, [field.name]: e.target.value }))}
              disabled={!hasPermission || loading}
            />
          </FormField>
        ))}
        <div><button className="btn" type="submit" disabled={!hasPermission || loading}>{loading ? '生成中…' : '开始生成'}</button></div>
      </form>
      {error ? <div className="alert error">{error}</div> : null}
      {result ? <div className="card" style={{ background: 'var(--panel-2)' }}><h3 style={{ marginTop: 0 }}>结果</h3><pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(result, null, 2)}</pre></div> : null}
    </div>
  )
}
