// src/features/tenants/users/pages/UserEdit.jsx
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useAppSelector } from '../../../../app/hooks'
import FormField from '../../../../components/ui/FormField.jsx'
import {
  getTenantMeta, getTenantUser, updateTenantUser,
  resetTenantUserPassword, deleteTenantUser,
} from '../service'

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

function Switch({checked, disabled, onToggle}) {
  const cls = 'switch' + (checked ? ' on' : '') + (disabled ? ' disabled' : '')
  return <div className={cls} onClick={()=>!disabled && onToggle()}><i/></div>
}

export default function UserEdit() {
  const nav = useNavigate()
  const { wid, uid } = useParams()
  const { isOwner, isAdmin } = useMyRole()

  const [metaName, setMetaName] = useState(`公司 ${wid || ''}`)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  const [id, setId] = useState(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('member')
  const [displayName, setDisplayName] = useState('')
  const [username, setUsername] = useState('')
  const [isActive, setIsActive] = useState(true)

  const canEdit       = useMemo(() => role !== 'owner' && (isOwner || (isAdmin && role==='member')), [isOwner, isAdmin, role])
  const canChangeRole = useMemo(() => role !== 'owner' && isOwner, [isOwner, role])
  const canToggleActive = useMemo(() => role !== 'owner' && (isOwner || (isAdmin && role==='member')), [isOwner, isAdmin, role])
  const canDelete     = canToggleActive
  const canResetPwd   = canToggleActive

  useEffect(() => {
    let abort = false
    ;(async () => {
      try {
        const m = await getTenantMeta(wid)
        if (!abort && m?.name) setMetaName(m.name)
      } catch {}
    })()
    return () => { abort = true }
  }, [wid])

  useEffect(() => {
    let abort = false
    ;(async () => {
      setLoading(true); setErr('')
      try {
        const u = await getTenantUser(wid, uid)
        if (abort) return
        setId(u.id); setEmail(u.email); setRole(u.role)
        setDisplayName(u.display_name || ''); setUsername(u.username || '')
        setIsActive(!!u.is_active)
      } catch (e) { if (!abort) setErr(e?.message || '加载失败') }
      finally { if (!abort) setLoading(false) }
    })()
    return () => { abort = true }
  }, [wid, uid])

  async function onSave() {
    if (!id) return
    const patch = { display_name: displayName || null, username: username || null }
    if (canChangeRole)   patch.role = role
    if (canToggleActive) patch.is_active = isActive
    try {
      const updated = await updateTenantUser(wid, id, patch)
      setRole(updated.role)
      setDisplayName(updated.display_name || '')
      setUsername(updated.username || '')
      setIsActive(!!updated.is_active)
      alert('已保存')
    } catch (e) { alert(e?.message || '保存失败') }
  }

  async function onResetPwd() {
    if (!id || !canResetPwd) return
    const np = prompt('输入新密码（至少8位）：')
    if (np === null) return
    if (!np || np.length < 8) return alert('密码长度不足 8 位')
    try { await resetTenantUserPassword(wid, id, np); alert('密码已重置') }
    catch (e) { alert(e?.message || '重置失败') }
  }

  async function onDelete() {
    if (!id || !canDelete) return
    if (!confirm(`确定删除成员「${username || email}」吗？`)) return
    try { await deleteTenantUser(wid, id); alert('已删除'); nav(`/tenants/${wid}/users`, { replace: true }) }
    catch (e) { alert(e?.message || '删除失败') }
  }

  return (
    <div className="card card--elevated" style={{maxWidth: 760}}>
      <div style={{display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:12}}>
        <h3 style={{margin:0}}>编辑成员（{metaName}）</h3>
        <Link className="btn ghost" to={`/tenants/${wid}/users`}>返回列表</Link>
      </div>

      {err && <div className="alert alert--error" style={{marginBottom:10}}>{err}</div>}
      {loading ? (
        <div className="card">加载中…</div>
      ) : (
        <>
          <div className="form-grid">
            <FormField label="邮箱（只读）">
              <input value={email} disabled />
            </FormField>

            <FormField label="角色">
              <RoleSeg value={role} onChange={setRole} disabled={!canChangeRole} />
              {!canChangeRole && <div className="small-muted" style={{marginTop:6}}>仅 owner 可修改角色</div>}
            </FormField>

            <FormField label="Display Name">
              <input value={displayName} onChange={e=>setDisplayName(e.target.value)} disabled={!canEdit} placeholder="可留空" />
            </FormField>

            <FormField label="用户名">
              <input value={username} onChange={e=>setUsername(e.target.value)} disabled={!canEdit} placeholder="可留空" />
            </FormField>

            <FormField label="状态">
              <div style={{display:'flex', alignItems:'center', gap:8}}>
                <Switch checked={!!isActive} disabled={!canToggleActive} onToggle={()=>setIsActive(v=>!v)} />
                <span className="small-muted">{isActive ? '启用' : '停用'}</span>
              </div>
            </FormField>
          </div>

          <div className="actions" style={{marginTop:12, display:'flex', gap:8, flexWrap:'wrap'}}>
            <button className="btn" onClick={onSave} disabled={!canEdit && !canChangeRole && !canToggleActive}>保存</button>
            {canResetPwd && <button className="btn ghost" onClick={onResetPwd}>重置密码</button>}
            {canDelete && <button className="btn danger" onClick={onDelete}>删除成员</button>}
          </div>
        </>
      )}
    </div>
  )
}

