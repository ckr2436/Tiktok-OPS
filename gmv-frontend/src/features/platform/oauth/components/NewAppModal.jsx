import { useEffect, useMemo, useState } from 'react'

import { useAppSelector } from '../../../../app/hooks.js'
import Modal from '../../../../components/ui/Modal.jsx'
import { parseBoolLike } from '../../../../utils/booleans.js'
import { upsertProviderApp } from '../../oauth/service.js'


function apiError(error) {
  const detail = error?.response?.data?.detail
  if (Array.isArray(detail)) return detail.map((item) => item?.msg || String(item)).join('；')
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  return error?.message || '提交失败。'
}


export default function NewAppModal({ open, onClose, onSaved, mode = 'create', initial = null }) {
  const me = useAppSelector((state) => state.session?.data)
  const canManage = parseBoolLike(me?.isPlatformAdmin ?? me?.is_platform_admin) && String(me?.role || '').toLowerCase() === 'owner'
  const isEdit = mode === 'edit'
  const [provider, setProvider] = useState('tiktok_business')
  const [name, setName] = useState('')
  const [clientId, setClientId] = useState('')
  const [serviceId, setServiceId] = useState('')
  const [redirectUri, setRedirectUri] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [rotateSecret, setRotateSecret] = useState(false)
  const [clientSecret, setClientSecret] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    if (isEdit && initial) {
      setProvider(initial.provider || 'tiktok_business')
      setName(initial.name || '')
      setClientId(initial.client_id || '')
      setServiceId(initial.service_id || '')
      setRedirectUri(initial.redirect_uri || '')
      setEnabled(Boolean(initial.is_enabled))
      setRotateSecret(false)
    } else {
      setProvider('tiktok_business')
      setName('')
      setClientId('')
      setServiceId('')
      setRedirectUri('https://gmv.myupona.com/api/oauth/tiktok-business/callback')
      setEnabled(true)
      setRotateSecret(true)
    }
    setClientSecret('')
    setSubmitting(false)
    setError('')
  }, [open, isEdit, initial])

  function changeProvider(value) {
    setProvider(value)
    if (!isEdit) {
      setRedirectUri(value === 'tiktok_shop'
        ? 'https://gmv.myupona.com/api/oauth/tiktok-shop/callback'
        : 'https://gmv.myupona.com/api/oauth/tiktok-business/callback')
    }
  }

  const canSubmit = useMemo(() => {
    if (!canManage || name.trim().length < 2 || clientId.trim().length < 4) return false
    if (!/^https:\/\/.+/i.test(redirectUri.trim())) return false
    if (provider === 'tiktok_shop' && serviceId.trim().length < 4) return false
    if (!isEdit || rotateSecret) return clientSecret.trim().length >= 8
    return true
  }, [canManage, name, clientId, redirectUri, provider, serviceId, isEdit, rotateSecret, clientSecret])

  async function submit(event) {
    event.preventDefault()
    if (!canSubmit || submitting) return
    setSubmitting(true)
    setError('')
    try {
      const result = await upsertProviderApp({
        provider,
        name: name.trim(),
        client_id: clientId.trim(),
        service_id: provider === 'tiktok_shop' ? serviceId.trim() : null,
        redirect_uri: redirectUri.trim(),
        client_secret: isEdit && !rotateSecret ? null : clientSecret,
        is_enabled: enabled,
      })
      await onSaved?.(result)
      onClose?.()
    } catch (requestError) {
      setError(apiError(requestError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal open={open} visible={open} onClose={onClose} title={isEdit ? '编辑 OAuth 应用' : '新建 OAuth 应用'}>
      <form onSubmit={submit} style={{ display: 'grid', gap: 13 }}>
        <Field label="应用类型" required>
          <select className="input" value={provider} onChange={(event) => changeProvider(event.target.value)} disabled={isEdit || !canManage || submitting}>
            <option value="tiktok_business">TikTok Business</option>
            <option value="tiktok_shop">TikTok Shop</option>
          </select>
        </Field>
        <Field label="名称" required>
          <input className="input" value={name} onChange={(event) => setName(event.target.value)} maxLength={128} disabled={!canManage || submitting} />
        </Field>
        <Field label={provider === 'tiktok_shop' ? 'App Key' : 'App ID'} required>
          <input className="input" value={clientId} onChange={(event) => setClientId(event.target.value)} maxLength={128} disabled={isEdit || !canManage || submitting} />
        </Field>
        {provider === 'tiktok_shop' && (
          <Field label="Service ID" required hint="Partner Center 应用详情中的 Service ID">
            <input className="input" value={serviceId} onChange={(event) => setServiceId(event.target.value)} maxLength={128} disabled={!canManage || submitting} />
          </Field>
        )}
        <Field label="Redirect URI" required hint="必须与平台后台完全一致">
          <input className="input" type="url" value={redirectUri} onChange={(event) => setRedirectUri(event.target.value)} maxLength={512} disabled={!canManage || submitting} />
        </Field>
        {isEdit && (
          <label className="checkbox">
            <input type="checkbox" checked={rotateSecret} onChange={(event) => setRotateSecret(event.target.checked)} disabled={!canManage || submitting} />
            <span>更换 App Secret</span>
          </label>
        )}
        {(!isEdit || rotateSecret) && (
          <Field label="App Secret" required hint="提交后加密保存，页面不会回显">
            <input className="input" type="password" value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} minLength={8} maxLength={512} autoComplete="new-password" disabled={!canManage || submitting} />
          </Field>
        )}
        <label className="checkbox">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} disabled={!canManage || submitting} />
          <span>启用应用</span>
        </label>
        {error && <div className="alert alert--error">{error}</div>}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 6 }}>
          <button type="button" className="btn ghost" onClick={onClose} disabled={submitting}>取消</button>
          <button type="submit" className="btn" disabled={!canSubmit || submitting}>{submitting ? '保存中...' : '保存'}</button>
        </div>
      </form>
    </Modal>
  )
}


function Field({ label, required, hint, children }) {
  return (
    <label style={{ display: 'grid', gap: 6 }}>
      <span style={{ fontWeight: 650 }}>{label}{required && <span style={{ color: '#dc2626' }}> *</span>}</span>
      {hint && <span className="small-muted">{hint}</span>}
      {children}
    </label>
  )
}

