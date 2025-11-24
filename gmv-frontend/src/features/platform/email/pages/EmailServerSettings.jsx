// src/features/platform/email/pages/EmailServerSettings.jsx
import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { fetchEmailSettings, saveEmailSettings, sendTestEmail } from '../service'

const DEFAULT_PORTS = {
  SSL: 443,
  STARTTLS: 587,
  NONE: 25,
}

export default function EmailServerSettings() {
  const settingsQuery = useQuery({
    queryKey: ['platform', 'email-settings'],
    queryFn: () => fetchEmailSettings(),
  })

  const [form, setForm] = useState({
    send_mode: 'SMTP',
    encryption: 'SSL',
    from_address: '',
    host: '',
    port: 443,
    auth_enabled: false,
    username: '',
    password: '',
  })
  const [testEmail, setTestEmail] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if (settingsQuery.data) {
      const d = settingsQuery.data
      setForm({
        send_mode: d.send_mode || 'SMTP',
        encryption: d.encryption || 'SSL',
        from_address: d.from_address || '',
        host: d.host || '',
        port: d.port || DEFAULT_PORTS[d.encryption] || 443,
        auth_enabled: !!d.auth_enabled,
        username: d.username || '',
        password: '',
      })
    }
  }, [settingsQuery.data])

  const saveMutation = useMutation({
    mutationFn: payload => saveEmailSettings(payload),
  })
  const testMutation = useMutation({
    mutationFn: email => sendTestEmail(email),
  })

  function onFieldChange(key, value) {
    setNotice('')
    setForm(prev => ({ ...prev, [key]: value }))
  }

  function onEncryptionChange(value) {
    setNotice('')
    setForm(prev => {
      const prevPort = prev.port
      const nextDefault = DEFAULT_PORTS[value] || prev.port
      const shouldReplace = !prevPort || prevPort === DEFAULT_PORTS[prev.encryption]
      return { ...prev, encryption: value, port: shouldReplace ? nextDefault : prevPort }
    })
  }

  async function onSave() {
    try {
      const payload = { ...form }
      if (!payload.auth_enabled) {
        payload.username = null
        payload.password = null
      }
      await saveMutation.mutateAsync(payload)
      setNotice('保存成功，可以尝试发送测试邮件。')
    } catch (e) {
      const msg = e?.response?.data?.detail || e?.message || '保存失败'
      setNotice(msg)
    }
  }

  async function onTest() {
    if (!testEmail) {
      setNotice('请填写要接收测试邮件的邮箱。')
      return
    }
    try {
      await testMutation.mutateAsync(testEmail)
      setNotice('测试邮件已尝试发送，请检查邮箱是否收到。')
    } catch (e) {
      const msg = e?.response?.data?.detail || e?.message || '发送失败'
      setNotice(msg)
    }
  }

  const busy = settingsQuery.isLoading || saveMutation.isLoading
  const testing = testMutation.isLoading

  return (
    <div className="card card--elevated" style={{ maxWidth: 860 }}>
      <h3 style={{ marginTop: 0 }}>电子邮件服务器</h3>
      <p className="small-muted">平台管理员可在此配置 SMTP 服务，用于发送系统通知或测试邮件。</p>

      {settingsQuery.error && (
        <div className="alert alert--error" role="alert">{settingsQuery.error?.message}</div>
      )}

      {notice && (
        <div className="alert" role="alert" style={{ marginBottom: 12 }}>
          {notice}
        </div>
      )}

      <div className="form-grid">
        <label className="form-field">
          <span className="form-label">发送模式</span>
          <select
            className="input"
            value={form.send_mode}
            onChange={e => onFieldChange('send_mode', e.target.value)}
            disabled={busy}
          >
            <option value="SMTP">SMTP</option>
          </select>
        </label>

        <label className="form-field">
          <span className="form-label">加密</span>
          <select
            className="input"
            value={form.encryption}
            onChange={e => onEncryptionChange(e.target.value)}
            disabled={busy}
          >
            <option value="SSL">SSL</option>
            <option value="STARTTLS">STARTTLS</option>
            <option value="NONE">无</option>
          </select>
          <span className="small-muted">SSL 默认端口 443，STARTTLS 默认端口 587，可自行修改。</span>
        </label>

        <label className="form-field">
          <span className="form-label">来自地址</span>
          <input
            className="input"
            value={form.from_address}
            onChange={e => onFieldChange('from_address', e.target.value)}
            placeholder="noreply@example.com"
            disabled={busy}
          />
        </label>

        <div className="form-field">
          <span className="form-label">服务器地址</span>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              className="input"
              style={{ flex: 1 }}
              value={form.host}
              onChange={e => onFieldChange('host', e.target.value)}
              placeholder="smtp.example.com"
              disabled={busy}
            />
            <input
              className="input"
              style={{ width: 120 }}
              type="number"
              value={form.port}
              onChange={e => onFieldChange('port', Number(e.target.value))}
              disabled={busy}
              min={1}
            />
          </div>
          <span className="small-muted">格式示例：smtp.example.com:443</span>
        </div>

        <label className="form-field" style={{ flexBasis: '100%' }}>
          <span className="form-label">身份认证（可选）</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
            <input
              id="auth_enabled"
              type="checkbox"
              checked={!!form.auth_enabled}
              onChange={e => onFieldChange('auth_enabled', e.target.checked)}
              disabled={busy}
            />
            <label htmlFor="auth_enabled">需要账号密码</label>
          </div>
          {form.auth_enabled && (
            <div className="form-grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <label className="form-field">
                <span className="form-label">账号</span>
                <input
                  className="input"
                  value={form.username}
                  onChange={e => onFieldChange('username', e.target.value)}
                  disabled={busy}
                  placeholder="user@example.com"
                />
              </label>
              <label className="form-field">
                <span className="form-label">密码</span>
                <input
                  className="input"
                  type="password"
                  value={form.password}
                  onChange={e => onFieldChange('password', e.target.value)}
                  disabled={busy}
                  placeholder="••••••"
                />
              </label>
            </div>
          )}
        </label>
      </div>

      <div style={{ display: 'flex', gap: 12, marginTop: 16, flexWrap: 'wrap', alignItems: 'center' }}>
        <button className="btn primary" onClick={onSave} disabled={busy}>
          {saveMutation.isLoading ? '保存中…' : '保存配置'}
        </button>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input
            className="input"
            placeholder="测试收件人邮箱"
            value={testEmail}
            onChange={e => setTestEmail(e.target.value)}
            style={{ width: 240 }}
            disabled={busy || testing}
          />
          <button className="btn" onClick={onTest} disabled={busy || testing}>
            {testing ? '发送中…' : '发送测试邮件'}
          </button>
        </div>
      </div>

      <div className="small-muted" style={{ marginTop: 12 }}>
        测试邮件将使用上方配置发送，若失败会返回错误信息；成功则请前往邮箱确认是否收到。
      </div>
    </div>
  )
}
