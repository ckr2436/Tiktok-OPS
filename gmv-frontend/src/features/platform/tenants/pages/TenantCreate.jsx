// src/features/platform/tenants/pages/TenantCreate.jsx
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createCompany } from '../service'

export default function TenantCreate() {
  const navigate = useNavigate()
  const [submitting, setSubmitting] = useState(false)
  const [errMsg, setErrMsg] = useState('')

  const [form, setForm] = useState({
    name: '',
    company_code: '',    // 4 位数字；留空后端自动分配
    owner_email: '',
    owner_password: '',
    owner_display_name: '',
    owner_username: '',  // 可留空，后端会从 email 推导并在工作区内去重
  })

  function onChange(k, v) {
    setForm(prev => ({ ...prev, [k]: v }))
  }

  async function onSubmit(e) {
    e.preventDefault()
    setErrMsg('')
    if (!form.name || !form.owner_email || !form.owner_password) {
      setErrMsg('公司名、Owner 邮箱、Owner 密码为必填')
      return
    }
    if (form.company_code && !/^\d{4}$/.test(form.company_code)) {
      setErrMsg('公司码必须是 4 位数字，或留空自动分配')
      return
    }

    const payload = {
      name: form.name,
      company_code: form.company_code || null,
      owner: {
        email: form.owner_email,
        password: form.owner_password,
        display_name: form.owner_display_name || null,
        username: form.owner_username || null,
      },
    }

    try {
      setSubmitting(true)
      const data = await createCompany(payload)
      alert(`创建成功：公司码 ${data.company_code}，Owner 用户码 ${data.owner_usercode}`)
      navigate('/platform/tenants')
    } catch (e) {
      setErrMsg(e?.response?.data?.detail || e?.message || '创建失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="card card--elevated">
      <h3 style={{marginTop:0}}>创建公司</h3>
      {errMsg && <div className="alert alert--error" style={{marginBottom:10}}>{errMsg}</div>}

      <form className="form" onSubmit={onSubmit}>
        <div className="form-field">
          <label className="form-field__label">公司名 *</label>
          <div className="form-field__control">
            <input className="input" value={form.name} onChange={e=>onChange('name', e.target.value)} placeholder="请输入公司名称" />
          </div>
        </div>

        <div className="form-field">
          <label className="form-field__label">公司码（4 位数字，留空自动分配）</label>
          <div className="form-field__control">
            <input className="input" value={form.company_code} onChange={e=>onChange('company_code', e.target.value)} placeholder="如 1234，可留空" />
          </div>
        </div>

        <div className="form-field">
          <label className="form-field__label">Owner 邮箱 *</label>
          <div className="form-field__control">
            <input className="input" value={form.owner_email} onChange={e=>onChange('owner_email', e.target.value)} placeholder="owner@example.com" />
          </div>
        </div>

        <div className="form-field">
          <label className="form-field__label">Owner 密码 *</label>
          <div className="form-field__control">
            <input type="password" className="input" value={form.owner_password} onChange={e=>onChange('owner_password', e.target.value)} placeholder="至少 8 位" />
          </div>
        </div>

        <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap:12}}>
          <div className="form-field">
            <label className="form-field__label">Owner Display Name（可选）</label>
            <div className="form-field__control">
              <input className="input" value={form.owner_display_name} onChange={e=>onChange('owner_display_name', e.target.value)} placeholder="可留空" />
            </div>
          </div>
          <div className="form-field">
            <label className="form-field__label">Owner 用户名（可选）</label>
            <div className="form-field__control">
              <input className="input" value={form.owner_username} onChange={e=>onChange('owner_username', e.target.value)} placeholder="可留空，默认从邮箱推导" />
            </div>
          </div>
        </div>

        <div className="actions" style={{marginTop:8}}>
          <button type="button" className="btn ghost" onClick={()=>history.back()} disabled={submitting}>取消</button>
          <button type="submit" className="btn" disabled={submitting}>创建</button>
        </div>
      </form>
    </div>
  )
}

