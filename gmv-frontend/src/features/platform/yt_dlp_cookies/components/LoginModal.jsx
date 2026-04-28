import { useEffect, useState } from 'react'
import Modal from '../../../../components/ui/Modal.jsx'
import FormField from '../../../../components/ui/FormField.jsx'
import { SITE_OPTIONS, saveCookies, siteLabel } from '../api.js'

function normalizeCookiesInput(raw) {
  const text = raw.trim()
  if (!text) throw new Error('Cookies JSON 不能为空')
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch (error) {
    throw new Error('请输入浏览器导出的 Cookies JSON 数组，例如 [{"name":"...","value":"...","domain":"..."}]')
  }
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error('Cookies 必须是非空 JSON 数组')
  }
  for (const item of parsed) {
    if (!item || typeof item !== 'object') throw new Error('每一项 Cookie 都必须是对象')
    if (!item.name || item.value === undefined || item.value === null) throw new Error('每一项 Cookie 都需要包含 name 和 value')
    if (!item.domain) throw new Error('每一项 Cookie 都需要包含 domain')
  }
  return JSON.stringify(parsed)
}

export default function LoginModal({
  open,
  defaultSite = 'tiktok',
  defaultLabel = '',
  onClose,
  onSuccess,
  onToast,
}) {
  const [form, setForm] = useState({
    site: defaultSite || 'tiktok',
    label: defaultLabel || '',
    cookiesText: '',
    isActive: true,
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setForm({
      site: defaultSite || 'tiktok',
      label: defaultLabel || '',
      cookiesText: '',
      isActive: true,
    })
    setError('')
  }, [open, defaultSite, defaultLabel])

  const handleClose = () => {
    if (saving) return
    onClose?.()
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (saving) return
    setError('')
    if (!form.label.trim()) {
      setError('备注名不能为空')
      return
    }
    let cookiesJson
    try {
      cookiesJson = normalizeCookiesInput(form.cookiesText)
    } catch (err) {
      setError(err?.message || 'Cookies 格式不正确')
      return
    }

    setSaving(true)
    try {
      const record = await saveCookies({
        site: form.site,
        label: form.label.trim(),
        cookies_json: cookiesJson,
        is_active: !!form.isActive,
      })
      onToast?.('Cookies 已保存', 'success')
      await onSuccess?.(record)
      onClose?.()
    } catch (err) {
      const msg =
        err?.response?.data?.detail ||
        err?.response?.data?.error_msg ||
        err?.message ||
        '保存失败，请检查 Cookies JSON 格式。'
      setError(typeof msg === 'string' ? msg : JSON.stringify(msg))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open={open} onClose={saving ? undefined : handleClose} title="手动输入 Cookies" escClosable={!saving}>
      <form className="form" onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div className="small-muted">
          请先在真实浏览器中登录对应平台，再使用 Cookie 导出插件导出 JSON 数组并粘贴到这里。支持 TikTok、抖音、YouTube。
        </div>
        {error && <div className="alert alert--error">{error}</div>}

        <FormField label="站点" required>
          <select
            className="input"
            value={form.site}
            onChange={(e) => setForm((prev) => ({ ...prev, site: e.target.value }))}
            disabled={saving}
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
            placeholder="如：TikTok 运营号 A"
            disabled={saving}
          />
        </FormField>

        <FormField label={`${siteLabel(form.site)} Cookies JSON`} required>
          <textarea
            className="input"
            value={form.cookiesText}
            onChange={(e) => setForm((prev) => ({ ...prev, cookiesText: e.target.value }))}
            placeholder='粘贴浏览器导出的 JSON 数组，例如 [{"name":"sessionid","value":"...","domain":".tiktok.com","path":"/"}]'
            disabled={saving}
            rows={12}
            style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', resize: 'vertical' }}
          />
        </FormField>

        <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <input
            type="checkbox"
            checked={form.isActive}
            onChange={(e) => setForm((prev) => ({ ...prev, isActive: e.target.checked }))}
            disabled={saving}
          />
          保存后立即启用
        </label>

        <div className="alert alert--info">
          只保存 Cookies JSON，不再弹出二维码扫码登录。请不要把 Cookies 发给无关人员，Cookies 等同于登录凭证。
        </div>

        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <button className="btn ghost" type="button" onClick={handleClose} disabled={saving}>
            取消
          </button>
          <button className="btn" type="submit" disabled={saving}>
            {saving ? '保存中…' : '保存 Cookies'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
