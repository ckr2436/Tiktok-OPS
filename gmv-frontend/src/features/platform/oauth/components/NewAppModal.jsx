// src/features/platform/oauth/components/NewAppModal.jsx
import { useEffect, useMemo, useState } from 'react'
import Modal from '../../../../components/ui/Modal.jsx'
import { upsertProviderApp } from '../../oauth/service.js'
import { useAppSelector } from '../../../../app/hooks.js'
import { parseBoolLike } from '../../../../utils/booleans.js'

/**
 * 通用弹窗：创建/编辑 Provider App
 * props:
 *  - open: boolean
 *  - onClose: () => void
 *  - onSaved: (resp) => void
 *  - mode: 'create' | 'edit'
 *  - initial: { id, name, client_id, redirect_uri, is_enabled }  // 编辑时传入
 */
export default function NewAppModal({ open, onClose, onSaved, mode = 'create', initial = null }) {
  const me = useAppSelector(s => s.session?.data)

  // 兼容 isPlatformAdmin / is_platform_admin
  const adminFlag = me?.isPlatformAdmin ?? me?.is_platform_admin
  const isPlatformAdmin = parseBoolLike(adminFlag)
  const isOwner = (me?.role || '').toLowerCase() === 'owner'
  const canOperate = isPlatformAdmin && isOwner  // 后端 upsert 要求平台 Owner

  const isEdit = mode === 'edit'

  const [name, setName] = useState('')
  const [clientId, setClientId] = useState('')
  const [redirectUri, setRedirectUri] = useState('')
  const [enabled, setEnabled] = useState(true)

  // secret 处理：创建必填；编辑默认不改密钥（需勾选后才显示输入框）
  const [rotateSecret, setRotateSecret] = useState(false)
  const [clientSecret, setClientSecret] = useState('')

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (open) {
      if (isEdit && initial) {
        setName(initial.name || '')
        setClientId(initial.client_id || '')
        setRedirectUri(initial.redirect_uri || '')
        setEnabled(!!initial.is_enabled)
        setRotateSecret(false)
        setClientSecret('')
      } else {
        // create
        setName('')
        setClientId('')
        setRedirectUri('')
        setEnabled(true)
        setRotateSecret(true) // 创建时必须填 secret，默认展开
        setClientSecret('')
      }
      setSubmitting(false)
      setError('')
    }
  }, [open, isEdit, initial])

  const canSubmit = useMemo(() => {
    if (!canOperate) return false
    const okBase =
      name.trim().length >= 2 &&
      clientId.trim().length >= 4 &&
      /^https?:\/\/.+/i.test(redirectUri.trim())
    if (!okBase) return false
    if (isEdit) {
      // 编辑：仅当勾选“更换密钥”才校验 secret
      if (rotateSecret) {
        return (clientSecret.trim().length >= 8)
      }
      return true
    }
    // 创建：必须填 secret
    return clientSecret.trim().length >= 8
  }, [canOperate, name, clientId, redirectUri, clientSecret, rotateSecret, isEdit])

  async function handleSubmit(e) {
    e?.preventDefault?.()
    if (!canSubmit || submitting) return
    setSubmitting(true)
    setError('')
    try {
      const payload = {
        name,
        client_id: clientId,
        redirect_uri: redirectUri,
        is_enabled: enabled,
      }
      if (isEdit) {
        // 编辑：只有勾选了才携带 client_secret，否则置为 null 表示不变
        payload.client_secret = rotateSecret ? clientSecret : null
      } else {
        // 创建：必须携带 client_secret
        payload.client_secret = clientSecret
      }

      const data = await upsertProviderApp(payload)
      onSaved?.(data)
      onClose?.()
    } catch (err) {
      const api = err?.response?.data
      const msg =
        api?.detail?.message ||
        api?.detail ||
        api?.message ||
        err?.message ||
        '提交失败'
      setError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal open={open} visible={open} onClose={onClose} title={isEdit ? 'Edit Provider App' : 'New Provider App'}>
      <form onSubmit={handleSubmit} style={{display:'grid', gap:12}}>
        {!canOperate && (
          <div className="alert alert--warning">
            仅 <b>平台 Owner</b> 可以创建/更新 Provider App（你当前无权限，按钮将被禁用）。
          </div>
        )}

        <Field label="Name" required hint="展示名，例如 TTB-Prod-US">
          <input
            className="input"
            value={name}
            onChange={e=>setName(e.target.value)}
            placeholder="TTB-Prod-US"
            required
            minLength={2}
            maxLength={128}
            disabled={!canOperate || submitting}
          />
        </Field>

        <Field label="Client ID (App ID)" required>
          <input
            className="input"
            value={clientId}
            onChange={e=>setClientId(e.target.value)}
            placeholder="应用 Client ID / App ID"
            required
            minLength={4}
            maxLength={128}
            disabled={!canOperate || submitting /* 强烈建议编辑时不要允许修改 Client ID；如需允许，移除此禁用 */}
          />
        </Field>

        <Field label="Redirect URI" required hint="必须与 TikTok 后台配置一致">
          <input
            className="input"
            value={redirectUri}
            onChange={e=>setRedirectUri(e.target.value)}
            placeholder="https://gmv.drafyn.com/api/oauth/tiktok-business/callback"
            required
            maxLength={512}
            inputMode="url"
            disabled={!canOperate || submitting}
          />
        </Field>

        <label className="checkbox" style={{marginTop:2}}>
          <input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)} disabled={!canOperate || submitting} />
          <span>Enabled</span>
        </label>

        {/* 密钥输入：创建必填；编辑通过开关控制 */}
        {isEdit ? (
          <>
            <label className="checkbox" style={{marginTop:4}}>
              <input type="checkbox" checked={rotateSecret} onChange={e=>setRotateSecret(e.target.checked)} disabled={!canOperate || submitting}/>
              <span>更换 Client Secret</span>
            </label>
            {rotateSecret && (
              <Field label="Client Secret" required hint="出于安全考虑不会回显旧值；填写即表示轮换密钥">
                <input
                  className="input"
                  type="password"
                  value={clientSecret}
                  onChange={e=>setClientSecret(e.target.value)}
                  placeholder="新的 Client Secret"
                  required
                  minLength={8}
                  maxLength={512}
                  autoComplete="new-password"
                  disabled={!canOperate || submitting}
                />
              </Field>
            )}
          </>
        ) : (
          <Field label="Client Secret" required hint="仅新建或更换时填写；不会回显">
            <input
              className="input"
              type="password"
              value={clientSecret}
              onChange={e=>setClientSecret(e.target.value)}
              placeholder="Client Secret"
              required
              minLength={8}
              maxLength={512}
              autoComplete="new-password"
              disabled={!canOperate || submitting}
            />
          </Field>
        )}

        {error && <div className="alert alert--error">{error}</div>}

        <div style={{display:'flex', justifyContent:'flex-end', gap:8, marginTop:8}}>
          <button type="button" className="btn ghost" onClick={onClose} disabled={submitting}>取消</button>
          <button type="submit" className="btn" disabled={!canSubmit || submitting}>
            {submitting ? '提交中…' : (isEdit ? '保存修改' : '创建')}
          </button>
        </div>
      </form>
    </Modal>
  )
}

function Field({label, required, hint, children}) {
  return (
    <div>
      <div style={{display:'flex', alignItems:'baseline', gap:8, marginBottom:6}}>
        <label className="label">
          {label}{required && <span style={{color:'#ef4444'}}> *</span>}
        </label>
        {hint && <span className="small-muted">{hint}</span>}
      </div>
      {children}
    </div>
  )
}

