import { useEffect, useState } from 'react'
import Modal from '../../../../components/ui/Modal.jsx'
import FormField from '../../../../components/ui/FormField.jsx'
import {
  SITE_OPTIONS,
  cancelBrowserSession,
  getBrowserOverview,
  getBrowserSession,
  saveCookies,
  siteLabel,
  startBrowserSession,
} from '../api.js'

function normalizeCookieItem(item) {
  if (!item || typeof item !== 'object') {
    return { cookie: null, reason: 'not_object' }
  }

  const name = typeof item.name === 'string' ? item.name.trim() : item.name
  const value = item.value
  const domain = typeof item.domain === 'string' ? item.domain.trim() : item.domain

  // Some Chrome cookie exporters include pseudo cookies / malformed rows with an empty
  // name. They are not valid Netscape cookie entries and yt-dlp cannot use them, so
  // drop them instead of blocking the whole import.
  if (!name) {
    return { cookie: null, reason: 'empty_name' }
  }

  if (value === undefined || value === null) {
    return { cookie: null, reason: 'missing_value' }
  }

  if (!domain) {
    return { cookie: null, reason: 'missing_domain' }
  }

  return {
    cookie: {
      ...item,
      name,
      value: String(value),
      domain,
      path: item.path || '/',
    },
    reason: null,
  }
}

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

  const validCookies = []
  const stats = {
    emptyName: 0,
    missingValue: 0,
    missingDomain: 0,
    notObject: 0,
  }

  for (const item of parsed) {
    const { cookie, reason } = normalizeCookieItem(item)
    if (cookie) {
      validCookies.push(cookie)
      continue
    }
    if (reason === 'empty_name') stats.emptyName += 1
    else if (reason === 'missing_value') stats.missingValue += 1
    else if (reason === 'missing_domain') stats.missingDomain += 1
    else stats.notObject += 1
  }

  if (validCookies.length === 0) {
    throw new Error('没有可保存的有效 Cookies，请重新导出。有效 Cookie 必须包含 name、value 和 domain。')
  }

  const hardInvalidCount = stats.missingValue + stats.missingDomain + stats.notObject
  if (hardInvalidCount > 0) {
    throw new Error(
      `Cookies 中有 ${hardInvalidCount} 项缺少 value/domain 或不是对象，请重新导出后再保存。空 name 项会自动忽略。`,
    )
  }

  return {
    cookiesJson: JSON.stringify(validCookies),
    droppedEmptyName: stats.emptyName,
    savedCount: validCookies.length,
  }
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
  const [mode, setMode] = useState('browser')
  const [devices, setDevices] = useState([])
  const [deviceId, setDeviceId] = useState('')
  const [browserSession, setBrowserSession] = useState(null)

  useEffect(() => {
    if (!open) return
    setForm({
      site: defaultSite || 'tiktok',
      label: defaultLabel || '',
      cookiesText: '',
      isActive: true,
    })
    setError('')
    setMode('browser')
    setBrowserSession(null)
    getBrowserOverview()
      .then((data) => {
        const online = (data?.devices || []).filter((item) => item?.online && item?.bound)
        setDevices(online)
        setDeviceId((current) => current || online[0]?.device_id || '')
      })
      .catch((err) => setError(err?.message || '读取浏览器桥设备失败'))
  }, [open, defaultSite, defaultLabel])

  useEffect(() => {
    if (!open || !browserSession?.session_id || !['awaiting_login', 'capture_pending'].includes(browserSession?.state)) return undefined
    const timer = setInterval(async () => {
      try {
        const next = await getBrowserSession(browserSession.session_id)
        setBrowserSession(next)
        if (next?.state === 'ready') {
          onToast?.('Cookies 已通过浏览器桥自动抓取并保存', 'success')
          await onSuccess?.(next)
          onClose?.()
        }
      } catch (err) {
        setError(err?.message || '读取抓取状态失败')
      }
    }, 2500)
    return () => clearInterval(timer)
  }, [open, browserSession?.session_id, browserSession?.state, onClose, onSuccess, onToast])

  const handleClose = async () => {
    if (saving) return
    if (browserSession?.session_id && ['awaiting_login', 'capture_pending'].includes(browserSession?.state)) {
      try { await cancelBrowserSession(browserSession.session_id) } catch (_) { /* best-effort stop */ }
    }
    onClose?.()
  }

  const handleBrowserStart = async () => {
    setError('')
    if (!form.label.trim()) return setError('备注名不能为空')
    if (!deviceId) return setError('请选择在线 Windows 浏览器桥设备')
    setSaving(true)
    try {
      const session = await startBrowserSession({
        device_id: deviceId,
        site: form.site,
        label: form.label.trim(),
      })
      setBrowserSession(session)
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || '启动浏览器抓取失败')
    } finally {
      setSaving(false)
    }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (saving) return
    setError('')
    if (!form.label.trim()) {
      setError('备注名不能为空')
      return
    }
    let normalized
    try {
      normalized = normalizeCookiesInput(form.cookiesText)
    } catch (err) {
      setError(err?.message || 'Cookies 格式不正确')
      return
    }

    setSaving(true)
    try {
      const record = await saveCookies({
        site: form.site,
        label: form.label.trim(),
        cookies_json: normalized.cookiesJson,
        is_active: !!form.isActive,
      })
      const suffix = normalized.droppedEmptyName
        ? `，已自动忽略 ${normalized.droppedEmptyName} 项空 name Cookie`
        : ''
      onToast?.(`Cookies 已保存，共 ${normalized.savedCount} 项${suffix}`, 'success')
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
    <Modal open={open} onClose={saving ? undefined : handleClose} title="更新 Cookies" escClosable={!saving}>
      <form className="form" onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div className="small-muted">
          推荐通过 Windows 浏览器桥自动抓取；也可继续手动粘贴浏览器导出的 JSON。
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" className={`btn ${mode === 'browser' ? '' : 'ghost'}`} onClick={() => setMode('browser')}>浏览器自动抓取</button>
          <button type="button" className={`btn ${mode === 'manual' ? '' : 'ghost'}`} onClick={() => setMode('manual')}>手动输入 JSON</button>
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

        {mode === 'browser' && <>
          <FormField label="Windows 浏览器桥" required>
            <select className="input" value={deviceId} onChange={(e) => setDeviceId(e.target.value)} disabled={saving || !!browserSession}>
              <option value="">请选择在线设备</option>
              {devices.map((device) => <option key={device.device_id} value={device.device_id}>{device.device_name || 'Windows device'} · {device.device_id}</option>)}
            </select>
          </FormField>
          {!devices.length && <div className="alert alert--info">当前没有在线且已绑定的 Hermes Bridge 设备。请先运行现有 Windows 浏览器桥。</div>}
          {browserSession && <div className="alert alert--info">
            <strong>{browserSession.state === 'capture_pending' ? '正在自动抓取' : '等待浏览器登录'}</strong>
            <div>{browserSession.message}</div>
            {browserSession.error && <div className="bad">{browserSession.error}</div>}
          </div>}
          <div className="alert alert--info">
            系统会在所选设备打开独立 Chrome Profile。完成登录并确认站点主页显示已登录后，关闭整个 Chrome 窗口；系统随后使用同一 Profile 短暂启动 CDP，仅抓取所选站点域名的 Cookies。
          </div>
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
            <button className="btn ghost" type="button" onClick={handleClose} disabled={saving}>取消</button>
            <button className="btn" type="button" onClick={handleBrowserStart} disabled={saving || !deviceId || !!browserSession}>
              {saving ? '启动中…' : browserSession ? '浏览器已启动' : '打开 Chrome 登录'}
            </button>
          </div>
        </>}

        {mode === 'manual' && <><FormField label={`${siteLabel(form.site)} Cookies JSON`} required>
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
          如果导出的 JSON 里存在浏览器插件生成的空 name 项，系统会自动忽略。
        </div>

        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <button className="btn ghost" type="button" onClick={handleClose} disabled={saving}>
            取消
          </button>
          <button className="btn" type="submit" disabled={saving}>
            {saving ? '保存中…' : '保存 Cookies'}
          </button>
        </div></>}
      </form>
    </Modal>
  )
}
