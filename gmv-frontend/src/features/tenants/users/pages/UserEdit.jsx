// src/features/tenants/users/pages/UserEdit.jsx
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useAppSelector } from '../../../../app/hooks'
import FormField from '../../../../components/ui/FormField.jsx'
import {
  getTenantMeta,
  getTenantUser,
  updateTenantUser,
  resetTenantUserPassword,
  deleteTenantUser,
} from '../service'

function useMyRole() {
  const me = useAppSelector(s => s.session?.data)
  const role = (me?.role || '').toLowerCase()
  return { me, isOwner: role === 'owner', isAdmin: role === 'admin' }
}

function RoleSeg({ value, onChange, disabled }) {
  return (
    <div className="seg">
      <button
        type="button"
        className={value === 'admin' ? 'on' : ''}
        onClick={() => onChange('admin')}
        disabled={disabled}
      >
        管理员
      </button>
      <button
        type="button"
        className={value === 'member' ? 'on' : ''}
        onClick={() => onChange('member')}
        disabled={disabled}
      >
        成员
      </button>
    </div>
  )
}

function Switch({ checked, disabled, onToggle }) {
  const cls = 'switch' + (checked ? ' on' : '') + (disabled ? ' disabled' : '')
  return (
    <div className={cls} onClick={() => !disabled && onToggle()}>
      <i />
    </div>
  )
}

export default function UserEdit() {
  const nav = useNavigate()
  const { wid, uid } = useParams()
  const { isOwner, isAdmin } = useMyRole()

  const [metaName, setMetaName] = useState(`公司 ${wid || ''}`)
  const [id, setId] = useState(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('member')
  const [displayName, setDisplayName] = useState('')
  const [username, setUsername] = useState('')
  const [isActive, setIsActive] = useState(true)

  const queryClient = useQueryClient()

  useQuery({
    queryKey: ['tenant-meta', wid],
    queryFn: () => getTenantMeta(wid),
    enabled: Boolean(wid),
    onSuccess: (meta) => {
      if (meta?.name) setMetaName(meta.name)
    },
  })

  const userQuery = useQuery({
    queryKey: ['tenant-user', wid, uid],
    queryFn: () => getTenantUser(wid, uid),
    enabled: Boolean(wid && uid),
  })

  useEffect(() => {
    const user = userQuery.data
    if (!user) return
    setId(user.id)
    setEmail(user.email)
    setRole(user.role)
    setDisplayName(user.display_name || '')
    setUsername(user.username || '')
    setIsActive(!!user.is_active)
  }, [userQuery.data])

  const canEdit = useMemo(
    () => role !== 'owner' && (isOwner || (isAdmin && role === 'member')),
    [isOwner, isAdmin, role],
  )
  const canChangeRole = useMemo(
    () => role !== 'owner' && isOwner,
    [isOwner, role],
  )
  const canToggleActive = useMemo(
    () => role !== 'owner' && (isOwner || (isAdmin && role === 'member')),
    [isOwner, isAdmin, role],
  )
  const canDelete = canToggleActive
  const canResetPwd = canToggleActive

  const updateMutation = useMutation({
    mutationFn: (patch) => updateTenantUser(wid, id, patch),
    onSuccess: (updated) => {
      setRole(updated.role)
      setDisplayName(updated.display_name || '')
      setUsername(updated.username || '')
      setIsActive(!!updated.is_active)
      queryClient.invalidateQueries({ queryKey: ['tenant-users', wid] })
      alert('已保存')
    },
    onError: (e) => {
      alert(e?.message || '保存失败')
    },
  })

  const resetMutation = useMutation({
    mutationFn: (password) => resetTenantUserPassword(wid, id, password),
    onSuccess: () => alert('密码已重置'),
    onError: (e) => alert(e?.message || '重置失败'),
  })

  const deleteMutation = useMutation({
    mutationFn: () => deleteTenantUser(wid, id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenant-users', wid] })
      alert('已删除')
      nav(`/tenants/${wid}/users`, { replace: true })
    },
    onError: (e) => alert(e?.message || '删除失败'),
  })

  async function onSave() {
    if (!id) return
    const patch = {
      display_name: displayName || null,
      username: username || null,
      role,
      is_active: isActive,
    }
    await updateMutation.mutateAsync(patch)
  }

  async function onResetPwd() {
    if (!id || !canResetPwd) return
    const np = prompt('输入新密码（至少8位）：')
    if (np === null) return
    if (!np || np.length < 8) return alert('密码长度不足 8 位')
    await resetMutation.mutateAsync(np)
  }

  async function onDelete() {
    if (!id || !canDelete) return
    if (!confirm(`确定删除成员「${username || email}」吗？`)) return
    await deleteMutation.mutateAsync()
  }

  const loadError = userQuery.error instanceof Error
    ? userQuery.error
    : (userQuery.error?.message ? new Error(userQuery.error.message) : null)

  return (
    <div className="card card--elevated" style={{ maxWidth: 760 }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 12,
        }}
      >
        <h3 style={{ margin: 0 }}>编辑成员（{metaName}）</h3>
        <Link className="btn ghost" to={`/tenants/${wid}/users`}>
          返回列表
        </Link>
      </div>

      {loadError && (
        <div className="alert alert--error" style={{ marginBottom: 10 }}>
          {loadError.message}
        </div>
      )}

      {userQuery.isLoading ? (
        <div className="card">加载中…</div>
      ) : (
        <>
          <div className="form-grid">
            <FormField label="邮箱（只读）">
              <input value={email} disabled />
            </FormField>

            <FormField label="角色">
              <RoleSeg value={role} onChange={setRole} disabled={!canChangeRole} />
              {!canChangeRole && (
                <div className="small-muted" style={{ marginTop: 6 }}>
                  仅 owner 可修改角色
                </div>
              )}
            </FormField>

            <FormField label="Display Name">
              <input
                value={displayName}
                onChange={e => setDisplayName(e.target.value)}
                disabled={!canEdit}
                placeholder="可留空"
              />
            </FormField>

            <FormField label="用户名">
              <input
                value={username}
                onChange={e => setUsername(e.target.value)}
                disabled={!canEdit}
                placeholder="可留空"
              />
            </FormField>

            <FormField label="状态">
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Switch
                  checked={!!isActive}
                  disabled={!canToggleActive}
                  onToggle={() => canToggleActive && setIsActive(v => !v)}
                />
                <span className="small-muted">{isActive ? '启用' : '停用'}</span>
              </div>
            </FormField>
          </div>

          <div className="form__actions" style={{ marginTop: 16, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <button className="btn" onClick={onSave} disabled={!canEdit}>
              {updateMutation.isPending ? '保存中…' : '保存'}
            </button>
            <button className="btn ghost" onClick={() => nav(`/tenants/${wid}/users`)}>
              返回列表
            </button>
            <button className="btn ghost" onClick={onResetPwd} disabled={!canResetPwd}>
              {resetMutation.isPending ? '重置中…' : '重置密码'}
            </button>
            <button className="btn danger" onClick={onDelete} disabled={!canDelete}>
              {deleteMutation.isPending ? '删除中…' : '删除成员'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}
