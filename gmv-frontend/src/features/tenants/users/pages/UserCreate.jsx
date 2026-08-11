// src/features/tenants/users/pages/UserCreate.jsx
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useAppSelector } from '../../../../app/hooks'
import FormField from '../../../../components/ui/FormField.jsx'
import { createTenantUser, getTenantMeta } from '../service'

function useMyRole() {
  const me = useAppSelector(s => s.session?.data)
  const role = (me?.role || '').toLowerCase()
  return { me, isOwner: role === 'owner', isAdmin: role === 'admin' }
}

function RoleSeg({value, onChange, disabled}) {
  return (
    <div className="seg">
      <button type="button" className={value==='admin' ? 'on' : ''} onClick={()=>onChange('admin')} disabled={disabled}>管理员</button>
      <button type="button" className={value==='member' ? 'on' : ''} onClick={()=>onChange('member')} disabled={disabled}>成员</button>
    </div>
  )
}

export default function UserCreate() {
  const nav = useNavigate()
  const { wid } = useParams()
  const { isOwner, isAdmin } = useMyRole()

  const [metaName, setMetaName] = useState(`公司 ${wid || ''}`)
  useQuery({
    queryKey: ['tenant-meta', wid],
    queryFn: () => getTenantMeta(wid),
    enabled: !!wid,
    onSuccess: meta => {
      if (meta?.name) {
        setMetaName(meta.name)
      }
    },
  })

  const noPerm = !(isOwner || isAdmin)

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('member')
  const [displayName, setDisplayName] = useState('')
  const [username, setUsername] = useState('')
  const [err, setErr] = useState('')

  const queryClient = useQueryClient()
  const createMutation = useMutation({
    mutationFn: payload => createTenantUser(wid, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenant-users', wid] })
    },
  })

  useEffect(() => {
    if (!email || username) return
    const at = email.indexOf('@'); if (at > 0) setUsername(email.slice(0, at))
  }, [email, username])

  const canSubmit = useMemo(() =>
    !!email && !!password && password.length >= 8 && (role === 'admin' || role === 'member'), [email, password, role])

  async function onSubmit(e) {
    e.preventDefault()
    if (noPerm || !canSubmit) return
    setErr('')
    try {
      await createMutation.mutateAsync({
        email, password, role,
        display_name: displayName || null,
        username: username || null,
      })
      alert('创建成功')
      nav(`/tenants/${wid}/users`, { replace: true })
    } catch (e) {
      const code = e?.payload?.error?.code
      setErr(code === 'EMAIL_EXISTS' ? '该邮箱已存在，请更换' : (e?.message || '创建失败'))
    }
  }

  return (
    <div className="card card--elevated" style={{maxWidth: 760}}>
      <div style={{display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:12}}>
        <h3 style={{margin:0}}>新增成员（{metaName}）</h3>
        <Link className="btn ghost" to={`/tenants/${wid}/users`}>返回列表</Link>
      </div>

      {noPerm && <div className="alert alert--error" style={{marginBottom:10}}>无权限：仅 owner / admin 可新增成员</div>}
      {err && <div className="alert alert--error" style={{marginBottom:10}}>{err}</div>}

      <form onSubmit={onSubmit} className="form">
        <div className="form-grid">
          <FormField label="邮箱（必填）">
            <input type="email" value={email} onChange={e=>setEmail(e.target.value)} placeholder="name@example.com" required />
          </FormField>

          <FormField label="初始密码（≥8位）">
            <input type="password" value={password} onChange={e=>setPassword(e.target.value)} placeholder="至少 8 位" required />
          </FormField>

          <FormField label="角色">
            <RoleSeg value={role} onChange={setRole} disabled={noPerm} />
          </FormField>

          <FormField label="Display Name">
            <input value={displayName} onChange={e=>setDisplayName(e.target.value)} placeholder="可留空" />
          </FormField>

          <FormField label="用户名">
            <input value={username} onChange={e=>setUsername(e.target.value)} placeholder="默认取邮箱 @ 前缀，可留空" />
          </FormField>
        </div>

        <div className="actions" style={{marginTop:12}}>
          <button className="btn" disabled={noPerm || !canSubmit || createMutation.isPending}>{createMutation.isPending ? '创建中…' : '创建'}</button>
          <Link className="btn ghost" to={`/tenants/${wid}/users`}>取消</Link>
        </div>
      </form>
    </div>
  )
}

