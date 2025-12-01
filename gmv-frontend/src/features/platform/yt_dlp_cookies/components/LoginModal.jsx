import { useEffect, useMemo, useRef, useState } from 'react'
import Modal from '../../../../components/ui/Modal.jsx'
import FormField from '../../../../components/ui/FormField.jsx'
import { SITE_OPTIONS, createLoginSession, getLoginSession, siteLabel } from '../api.js'

const TERMINAL_STATUS = ['success', 'failed', 'expired']

function StatusBadge({ status }) {
  const map = {
    qrcode_ready: { text: '等待扫码', color: '#2563eb' },
    success: { text: '登录成功', color: '#16a34a' },
    failed: { text: '登录失败', color: '#ef4444' },
    expired: { text: '已过期', color: '#9ca3af' },
  }
  const conf = map[status] || { text: status || '未开始', color: '#6b7280' }
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: conf.color, fontWeight: 600 }}>
      <span style={{ width: 10, height: 10, borderRadius: '50%', background: conf.color }} />
      {conf.text}
    </span>
  )
}

export default function LoginModal({ open, defaultSite = 'tiktok', defaultLabel = '', onClose, onSuccess }) {
  const [form, setForm] = useState({ site: defaultSite || 'tiktok', label: defaultLabel || '' })
  const [session, setSession] = useState(null)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const pollTimer = useRef(null)

  useEffect(() => {
    if (!open) return
    setForm({ site: defaultSite || 'tiktok', label: defaultLabel || '' })
    setSession(null)
    setError('')
    setPollError('')
  }, [open, defaultSite, defaultLabel])

  useEffect(() => {
    if (!open) return () => {}
    if (!session?.login_session_id || TERMINAL_STATUS.includes(session.status)) return () => {}

    pollTimer.current && clearInterval(pollTimer.current)
    pollTimer.current = setInterval(async () => {
      try {
        const data = await getLoginSession(session.login_session_id)
        setSession(data)
        setPollError('')
      } catch (err) {
        const msg = err?.message || '查询登录状态失败'
        setPollError(msg)
      }
    }, 2500)

    return () => {
      pollTimer.current && clearInterval(pollTimer.current)
    }
  }, [open, session?.login_session_id, session?.status])

  useEffect(() => {
    if (!open) return undefined
    if (!session || session.status !== 'success') return undefined
    const timer = setTimeout(() => {
      onSuccess?.(session)
      onClose?.()
    }, 800)
    return () => clearTimeout(timer)
  }, [open, session, onClose, onSuccess])

  const status = session?.status || 'form'
  const qrcode = session?.qrcode_image_base64
  const account = session?.account

  const handleClose = () => {
    pollTimer.current && clearInterval(pollTimer.current)
    onClose?.()
  }

  const startLogin = async (payload) => {
    setCreating(true)
    setError('')
    setPollError('')
    try {
      const data = await createLoginSession(payload)
      setSession(data)
    } catch (err) {
      const msg =
        err?.response?.data?.detail ||
        err?.response?.data?.error_msg ||
        err?.message ||
        '请求失败，请稍后再试。'
      setError(msg)
    } finally {
      setCreating(false)
    }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (creating) return
    if (!form.label.trim()) {
      setError('备注名不能为空')
      return
    }
    await startLogin({ site: form.site, label: form.label.trim() })
  }

  const handleRetry = async () => {
    await startLogin({ site: form.site, label: form.label.trim() })
  }

  const tips = useMemo(() => {
    if (status === 'qrcode_ready') return '请使用对应 App 扫码并在手机上确认登录。'
    if (status === 'success') return '登录成功，正在刷新列表…'
    if (status === 'expired') return '二维码已过期，请重新生成。'
    if (status === 'failed') return '登录失败，请稍后重试或重新生成二维码。'
    return '提交后将启动 Playwright 打开登录页并截取二维码用于扫码登录。'
  }, [status])

  return (
    <Modal open={open} onClose={creating ? undefined : handleClose} title="扫码登录 / 刷新 Cookies" escClosable={!creating}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div className="small-muted">{tips}</div>
        {error && <div className="alert alert--error">{error}</div>}
        {pollError && <div className="alert alert--warning">{pollError}</div>}

        {status === 'form' && (
          <form className="form" onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <FormField label="站点" required>
              <select
                className="input"
                value={form.site}
                onChange={(e) => setForm((prev) => ({ ...prev, site: e.target.value }))}
                disabled={creating}
              >
                {SITE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </FormField>
            <FormField label="账号备注" required>
              <input
                className="input"
                value={form.label}
                onChange={(e) => setForm((prev) => ({ ...prev, label: e.target.value }))}
                placeholder="如：运营号 A"
                disabled={creating}
              />
            </FormField>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <button className="btn ghost" type="button" onClick={handleClose} disabled={creating}>
                取消
              </button>
              <button className="btn" type="submit" disabled={creating}>
                {creating ? '生成中…' : '开始扫码登录'}
              </button>
            </div>
          </form>
        )}

        {status !== 'form' && (
          <div style={{ display: 'grid', gap: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
              <div style={{ fontWeight: 700 }}>
                {siteLabel(form.site)} · {form.label}
              </div>
              <StatusBadge status={status} />
            </div>

            {qrcode ? (
              <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                <img
                  src={qrcode}
                  alt="登录二维码"
                  style={{ width: 260, height: 260, borderRadius: 12, border: '1px solid var(--border)', objectFit: 'contain' }}
                />
                <div className="small-muted">
                  打开对应 App 扫码并在手机上确认即可。
                  <br />
                  登录成功后会自动写入数据库并刷新列表。
                </div>
              </div>
            ) : (
              <div className="alert alert--info">二维码生成中，请稍候…</div>
            )}

            {account && (
              <div className="alert alert--success">
                已更新账号：{account.label || account.id}（{siteLabel(account.site)}），最近登录时间：
                {account.last_login_at ? ` ${new Date(account.last_login_at).toLocaleString()}` : ' 刚刚'}
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
              <button className="btn ghost" onClick={handleClose}>
                关闭
              </button>
              {(status === 'failed' || status === 'expired') && (
                <button className="btn" onClick={handleRetry} disabled={creating}>
                  重新生成二维码
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </Modal>
  )
}
