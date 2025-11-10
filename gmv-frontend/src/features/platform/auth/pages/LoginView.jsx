// src/features/platform/auth/pages/LoginView.jsx
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import auth from '../service.js'
import { useAppDispatch, useAppSelector } from '../../../../app/hooks.js'
import { setSession } from '../sessionSlice.js'
import MinimalLayout from '../../../../components/layout/MinimalLayout.jsx'
import InitOwnerModal from './InitOwnerModal.jsx'
import FormField from '../../../../components/ui/FormField.jsx'
import Modal from '../../../../components/ui/Modal.jsx'
import Doc from '../../../../components/ui/Doc.jsx'
import { useAdminExistsQuery, useSessionQuery } from '../hooks.js'

function EyeButton({ on, onToggle }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={on ? '隐藏密码' : '显示密码'}
      title={on ? '隐藏密码' : '显示密码'}
      style={{
        position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)',
        background: 'transparent', border: 'none', cursor: 'pointer', padding: 4, borderRadius: 6
      }}
    >
      {on ? (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M3 3l18 18" />
          <path d="M17.94 17.94C16.13 19.26 14.14 20 12 20 6 20 2 12 2 12a22.4 22.4 0 0 1 6.06-6.06" />
          <path d="M9.88 9.88A3 3 0 0 0 12 15c.32 0 .62-.05.9-.14" />
        </svg>
      ) : (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      )}
    </button>
  )
}

function TenantPickerModal({ open, onClose, username, candidates = [], onPick, submitting }) {
  const [wid, setWid] = useState(null)

  useEffect(() => { if (open) setWid(null) }, [open])
  const canSubmit = useMemo(() => !!wid, [wid])

  return (
    <Modal open={open} onClose={onClose} title="选择登录公司">
      <div className="small-muted" style={{ marginBottom: 10 }}>
        你输入的用户名 <b>{username}</b> 在多个公司中存在，请选择要登录的公司：
      </div>

      {/* ❗ 关键：包一层 .table-wrap 以便移动端横向滚动 */}
      <div className="table-wrap" style={{ border: '1px solid var(--border)', borderRadius: 10 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 520 }}>
          <thead style={{ background: 'var(--panel-2)' }}>
            <tr>
              <th style={{ textAlign: 'left', padding: '8px 10px', width: 80 }}>选择</th>
              <th style={{ textAlign: 'left', padding: '8px 10px' }}>公司名称</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', width: 120 }}>公司代码</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', width: 160 }}>Workspace ID</th>
            </tr>
          </thead>
          <tbody>
            {candidates.length === 0 ? (
              <tr><td colSpan={4} style={{ padding: 12, color: 'var(--muted)' }}>未发现可用公司</td></tr>
            ) : candidates.map(c => (
              <tr key={c.workspace_id} style={{ borderTop: '1px solid var(--border)' }}>
                <td style={{ padding: '8px 10px' }}>
                  <label className="radio">
                    <input
                      type="radio"
                      name="wid"
                      value={c.workspace_id}
                      checked={String(wid || '') === String(c.workspace_id)}
                      onChange={() => setWid(c.workspace_id)}
                    />
                    <span />
                  </label>
                </td>
                <td style={{ padding: '8px 10px' }}>{c.company_name || '-'}</td>
                <td style={{ padding: '8px 10px' }}><code>{c.company_code}</code></td>
                <td style={{ padding: '8px 10px' }}><code>{c.workspace_id}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
        <button className="btn ghost" type="button" onClick={onClose} disabled={submitting}>取消</button>
        <button className="btn" type="button" disabled={!canSubmit || submitting} onClick={() => canSubmit && onPick?.(wid)}>
          {submitting ? '提交中…' : '确认并登录'}
        </button>
      </div>
    </Modal>
  )
}

export default function LoginView() {
  const nav = useNavigate()
  const location = useLocation()
  const from = (location.state && location.state.from) || '/dashboard'

  useSessionQuery()
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const session = useAppSelector(s => s.session.data)
  const checked = useAppSelector(s => s.session.checked)
  const adminExistsQuery = useAdminExistsQuery({ enabled: checked && !session })

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPwd, setShowPwd] = useState(false)
  const [remember, setRemember] = useState(() => {
    const v = localStorage.getItem('gmv.remember')
    return v === null ? true : v === '1' // 默认开启自动登录
  })
  const [error, setError] = useState('')
  const [showInit, setShowInit] = useState(false)
  const [shake, setShake] = useState(false)

  // Markdown 文档弹窗：'terms' | 'privacy' | null
  const [docOpen, setDocOpen] = useState(null)

  // 选择租户弹窗
  const [pickOpen, setPickOpen] = useState(false)
  const [candidates, setCandidates] = useState([])
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    const savedName = localStorage.getItem('gmv.username')
    if (savedName) setUsername(savedName)
  }, [])

  useEffect(() => {
    if (checked && session) {
      nav(from, { replace: true })
    }
  }, [checked, session, nav, from])

  useEffect(() => {
    if (session) {
      setShowInit(false)
    }
  }, [session])

  useEffect(() => {
    if (adminExistsQuery.data === false) {
      setShowInit(true)
    }
  }, [adminExistsQuery.data])

  useEffect(() => {
    if (adminExistsQuery.isError) {
      setShowInit(false)
    }
  }, [adminExistsQuery.isError])

  async function performLogin({ workspace_id } = {}) {
    setError('')
    setSubmitting(true)
    try {
      const s = await auth.login({ username, password, remember, workspace_id })
      // 保存“自动登录”偏好与用户名（不保存密码）
      if (remember) {
        localStorage.setItem('gmv.remember', '1')
        localStorage.setItem('gmv.username', username)
      } else {
        localStorage.setItem('gmv.remember', '0')
        localStorage.removeItem('gmv.username')
      }
      queryClient.setQueryData(['platform', 'session'], s)
      dispatch(setSession(s))
      nav(from, { replace: true })
      return true
    } catch (err) {
      // 统一转成友好错误
      const code = err?.payload?.error?.code || err?.response?.data?.error?.code || ''
      const msg =
        code === 'AUTH_FAILED' ? '用户名或密码不正确' :
        err?.message || '登录失败，请稍后再试'
      setError(msg)
      setShake(true); setTimeout(() => setShake(false), 420)

      // 仅当后端返回 AUTH_FAILED 时进行租户发现（避免把其它错误也当成多租户问题）
      if (code === 'AUTH_FAILED') {
        try {
          const list = await auth.discoverTenants(username)
          if (Array.isArray(list) && list.length >= 2) {
            setCandidates(list)
            setPickOpen(true)
            return false
          }
        } catch {/* 静默 */}
      }
      return false
    } finally {
      setSubmitting(false)
    }
  }

  const onSubmit = async (e) => {
    e.preventDefault()
    // 第一次直接尝试登录（后端若唯一密码命中会直接成功；否则前端再触发发现）
    await performLogin()
  }

  if (session) return null
  if (!checked) {
    return (
      <MinimalLayout showDocs={false}>
        <div className="card">加载中...</div>
      </MinimalLayout>
    )
  }

  return (
    <MinimalLayout showDocs={false}>
      <div className={'login-card card card--elevated' + (shake ? ' shake' : '')} style={{ width: 'min(560px,92vw)' }}>
        <h2 style={{ margin: '0 0 12px 0' }}>登录 GMV</h2>

        <form onSubmit={onSubmit} className="form">
          <FormField label="用户名">
            <input
              value={username}
              onChange={e => setUsername(e.target.value)}
              placeholder="admin 或邮箱/用户名"
              required
              autoComplete="username"
            />
          </FormField>

          <FormField label="密码">
            <div style={{ position: 'relative' }}>
              <input
                type={showPwd ? 'text' : 'password'}
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="请输入密码"
                required
                autoComplete={remember ? 'current-password' : 'off'}
                style={{ width: '100%', paddingRight: 42 }}
              />
              <EyeButton on={showPwd} onToggle={() => setShowPwd(v => !v)} />
            </div>
            <div className="small-muted" style={{ marginTop: 6 }}>
              忘记密码？请联系管理员 <a href="mailto:support@drafyn.com">support@drafyn.com</a>
            </div>
          </FormField>

          {/* 自动登录（默认选中） */}
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14 }}>
            <input
              type="checkbox"
              checked={remember}
              onChange={e => setRemember(e.target.checked)}
              style={{ width: 16, height: 16 }}
            />
            <span>自动登录</span>
          </label>

          {/* 行业通用声明 —— 点击打开 Markdown 弹窗 */}
          <div className="small-muted" style={{ marginTop: 6, textAlign: 'center' }}>
            登录即代表你已阅读并同意{' '}
            <a href="#" onClick={(e) => { e.preventDefault(); setDocOpen('terms') }}>《服务条款》</a> 与{' '}
            <a href="#" onClick={(e) => { e.preventDefault(); setDocOpen('privacy') }}>《隐私政策》</a>
          </div>

          {error && <div className="alert alert--error">{error}</div>}

          <div className="actions">
            <button className="btn" disabled={submitting}>登录</button>
          </div>
        </form>
      </div>

      {/* 初始化 Owner */}
      <InitOwnerModal
        open={showInit}
        onClose={() => setShowInit(false)}
        onDone={() => {
          setShowInit(false)
          adminExistsQuery.refetch()
        }}
      />

      {/* 选择租户弹窗 */}
      <TenantPickerModal
        open={pickOpen}
        onClose={() => setPickOpen(false)}
        username={username}
        candidates={candidates}
        submitting={submitting}
        onPick={async (wid) => {
          // 选择租户后，携带 workspace_id 再次登录
          const ok = await performLogin({ workspace_id: wid })
          if (ok) setPickOpen(false)
        }}
      />

      {/* Markdown 文档弹窗 */}
      <Modal
        open={!!docOpen}
        onClose={() => setDocOpen(null)}
        title={docOpen === 'terms' ? '服务条款' : '隐私政策'}
      >
        <Doc kind={docOpen} />
      </Modal>
    </MinimalLayout>
  )
}

