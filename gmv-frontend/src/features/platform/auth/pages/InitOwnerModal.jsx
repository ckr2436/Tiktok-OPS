// src/features/platform/auth/pages/InitOwnerModal.jsx
import { useMemo, useState } from 'react'
import Modal from '../../../../components/ui/Modal.jsx'
import FormField from '../../../../components/ui/FormField.jsx'
import Doc from '../../../../components/ui/Doc.jsx'
import auth from '../service.js'

function scorePassword(pw){
  if(!pw) return 0
  let s = 0
  s += Math.min(6, Math.floor(pw.length/2))
  s += (/[a-z]/.test(pw) + /[A-Z]/.test(pw) + /\d/.test(pw) + /[^A-Za-z0-9]/.test(pw)) * 2
  if (!/(\w)\1{2,}/.test(pw)) s += 2
  if (pw.length < 8) return 1
  if (s < 8) return 1
  if (s < 12) return 2
  if (s < 16) return 3
  return 4
}

function EyeIcon({ open=false }) {
  return open ? (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <path d="M3 3l18 18"/><path d="M17.94 17.94C16.13 19.26 14.14 20 12 20 6 20 2 12 2 12a22.4 22.4 0 0 1 6.06-6.06"/>
      <path d="M9.88 9.88A3 3 0 0 0 12 15c.32 0 .62-.05.9-.14"/>
    </svg>
  ) : (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>
    </svg>
  )
}

export default function InitOwnerModal({ open, onClose, onDone }){
  const [email, setEmail] = useState('')
  const [p1, setP1] = useState('')
  const [p2, setP2] = useState('')
  const [showPwd, setShowPwd] = useState(false)
  const [accept, setAccept] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [docOpen, setDocOpen] = useState(null) // 'terms' | 'privacy'
  const [shake, setShake] = useState(false)

  const bars = useMemo(()=>scorePassword(p1), [p1])
  const emailOk = /\S+@\S+\.\S+/.test(email)
  const same = !!p1 && p1 === p2
  const canSubmit = open && emailOk && same && bars >= 2 && accept && !busy
  const color = bars>=4 ? '#16a34a' : bars>=3 ? '#22c55e' : bars>=2 ? '#f59e0b' : '#ef4444'

  const submit = async ()=>{
    if (!canSubmit) return
    setBusy(true); setError('')
    try{
      await auth.initPlatformOwner({ email: email.trim(), password: p1 })
      onDone?.(); onClose?.()
    }catch(e){
      const friendly =
        e?.payload?.error?.code === 'ALREADY_INITIALIZED'
          ? '平台已完成初始化，请直接登录。'
          : (e?.message || '创建失败，请稍后重试')
      setError(friendly)
      setShake(true); setTimeout(()=>setShake(false), 420)
    }finally{
      setBusy(false)
    }
  }

  return (
    <>
      <Modal open={open} onClose={()=>{/* 禁止遮罩关闭 */}} title="初始化平台 Owner">
        {/* 居中 + 响应式宽度 */}
        <div
          className={'init-card ' + (shake ? 'shake' : '')}
          style={{display:'grid', gap:12, width:'min(560px, 92vw)', margin:'0 auto'}}
        >
          <div className="chip" style={{alignSelf:'flex-start'}}>首次使用 · 必填</div>

          <FormField label="邮箱">
            <input className="input" value={email} onChange={e=>setEmail(e.target.value)} placeholder="admin@example.com" autoComplete="username" />
          </FormField>

          <FormField label="密码">
            <div className="input-wrap">
              <input
                className="input"
                type={showPwd ? 'text' : 'password'}
                value={p1}
                onChange={e=>setP1(e.target.value)}
                placeholder="至少 8 位，建议大小写 + 数字 + 符号"
                autoComplete="new-password"
              />
              <button
                type="button"
                className="eye-btn"
                onClick={()=>setShowPwd(v=>!v)}
                aria-label={showPwd ? '隐藏密码' : '显示密码'}
                title={showPwd ? '隐藏密码' : '显示密码'}
              >
                <EyeIcon open={showPwd} />
              </button>
            </div>
          </FormField>

          <FormField label="确认密码" error={!same && p2 ? '两次输入的密码不一致' : ''}>
            <input className="input" type="password" value={p2} onChange={e=>setP2(e.target.value)} placeholder="再次输入密码" autoComplete="new-password" />
          </FormField>

          {/* 强度条 */}
          <div style={{display:'flex', justifyContent:'flex-end'}}>
            <div style={{minWidth:220, maxWidth:280}}>
              <div className="pw-bars">
                {Array.from({length:4}).map((_,i)=>(
                  <div key={i} className={'pw-bar' + (i < bars ? ' on' : '')}>
                    <i style={{background: i < bars ? color : 'var(--border)'}} />
                  </div>
                ))}
              </div>
              <div className="small-muted" style={{marginTop:6, textAlign:'right'}}>
                密码强度：{['太短','弱','中','较强','强'][bars] || '太短'}
              </div>
            </div>
          </div>

          <label style={{display:'flex',alignItems:'center',gap:10, fontSize:14}}>
            <input type="checkbox" checked={accept} onChange={e=>setAccept(e.target.checked)} style={{width:16,height:16}}/>
            <span>
              我已阅读并同意{' '}
              <a href="#" onClick={(e)=>{e.preventDefault(); setDocOpen('terms')}}>《服务条款》</a>{' '}与{' '}
              <a href="#" onClick={(e)=>{e.preventDefault(); setDocOpen('privacy')}}>《隐私政策》</a>
            </span>
          </label>

          {error && <div className="alert alert--error" role="alert">{error}</div>}

          <div className="actions">
            <button className="btn" disabled={!canSubmit} onClick={submit} style={{minWidth:120}}>
              {busy ? '创建中…' : '创建'}
            </button>
          </div>
        </div>
      </Modal>

      <Modal open={!!docOpen} onClose={()=>setDocOpen(null)} title={docOpen === 'terms' ? '服务条款' : '隐私政策'}>
        <Doc kind={docOpen} />
      </Modal>
    </>
  )
}

