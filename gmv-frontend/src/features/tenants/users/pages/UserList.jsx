// src/features/tenants/users/pages/UserList.jsx
import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useAppSelector } from '../../../../app/hooks'
import { listTenantUsers, getTenantMeta, updateTenantUser } from '../service'

function useMyRole() {
  const me = useAppSelector(s => s.session?.data)
  const role = (me?.role || '').toLowerCase()
  return { me, isOwner: role === 'owner', isAdmin: role === 'admin' }
}

function RoleBadgeCN({ role }) {
  const r = (role || '').toLowerCase()
  const label = r === 'owner' ? 'Owner' : (r === 'admin' ? '管理员' : '成员')
  const bg = r === 'owner' ? '#f97316' : r === 'admin' ? '#2563eb' : '#10b981'
  return <span className="badge-role" style={{ background: bg }}>{label}</span>
}

function Switch({ checked, disabled, onToggle }) {
  const cls = 'switch' + (checked ? ' on' : '') + (disabled ? ' disabled' : '')
  return <div className={cls} onClick={() => !disabled && onToggle()}><i /></div>
}

export default function UserList() {
  const { wid } = useParams()
  const { isOwner, isAdmin } = useMyRole()

  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [size] = useState(20)
  const [loading, setLoading] = useState(false)
  const [rows, setRows] = useState([])
  const [total, setTotal] = useState(0)
  const [err, setErr] = useState('')
  const [companyName, setCompanyName] = useState(`公司 ${wid || ''}`)

  const totalPages = useMemo(() => Math.max(1, Math.ceil((total || 0) / size)), [total, size])

  useEffect(() => {
    let abort = false
    async function loadMeta() {
      if (!wid) return
      try {
        const meta = await getTenantMeta(wid)
        if (!abort && meta?.name) setCompanyName(meta.name)
      } catch {
        if (!abort) setCompanyName(`公司 ${wid}`)
      }
    }
    loadMeta()
    return () => { abort = true }
  }, [wid])

  async function load() {
    if (!wid) return
    setLoading(true); setErr('')
    try {
      const data = await listTenantUsers({ wid, q, page, size })
      setRows(data.items || [])
      setTotal(data.total || 0)
    } catch (e) {
      setErr(e?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [wid, q, page, size])

  function canToggleActive(t) {
    if (!t) return false
    if (t.role === 'owner') return false
    return isOwner || (isAdmin && t.role === 'member')
  }

  async function onToggleActive(target) {
    if (!canToggleActive(target)) return
    try {
      const updated = await updateTenantUser(wid, target.id, { is_active: !target.is_active })
      setRows(list => list.map(x => x.id === target.id ? { ...x, ...updated } : x))
    } catch (e) { alert(e?.message || '切换状态失败') }
  }

  return (
    <div className="card card--elevated">
      <div style={{display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:12}}>
        <div>
          <h3 style={{margin:0}}>成员（{companyName}）</h3>
          <div className="small-muted">接口：GET /tenants/{'{wid}'}/users</div>
        </div>
        <div style={{display:'flex', gap:8}}>
          <input
            className="input"
            placeholder="搜索 用户名 / 邮箱 / 用户码"
            value={q}
            onChange={e => { setPage(1); setQ(e.target.value.trim()) }}
            style={{width:260}}
          />
          {(isOwner || isAdmin) && (
            <Link className="btn" to={`/tenants/${wid}/users/create`}>+ 新增成员</Link>
          )}
        </div>
      </div>

      {err && <div className="alert alert--error" style={{marginBottom:10}}>{err}</div>}

      {/* ❗ table-wrap 开启横向滚动 */}
      <div className="table-wrap" style={{border:'1px solid var(--border)', borderRadius:12}}>
        <table style={{width:'100%', borderCollapse:'collapse', minWidth: 720}}>
          <thead style={{background:'var(--panel-2)'}}>
            <tr>
              <Th w={220}>姓名</Th>
              <Th w={180}>用户名</Th>
              <Th w={140}>用户码</Th>
              <Th w={120}>角色</Th>
              <Th w={140}>状态</Th>
              <Th w={160}>操作</Th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={6} style={{padding:22}}>加载中…</td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={6} style={{padding:18, color:'var(--muted)'}}>暂无数据</td></tr>
            ) : rows.map(r => (
              <tr key={r.id} style={{borderTop:'1px solid var(--border)'}}>
                <Td>{r.display_name || <span className="small-muted">（未设置）</span>}</Td>
                <Td>{r.username || <span className="small-muted">（未设置）</span>}</Td>
                <Td mono>{r.usercode}</Td>
                <Td><RoleBadgeCN role={r.role} /></Td>
                <Td>
                  <div style={{display:'flex', alignItems:'center', gap:8}}>
                    <Switch checked={!!r.is_active} disabled={!canToggleActive(r)} onToggle={() => onToggleActive(r)} />
                    <span className="small-muted">{r.is_active ? '启用' : '停用'}</span>
                  </div>
                </Td>
                <Td>
                  <div className="table-actions">
                    <Link className="btn sm ghost" to={`/tenants/${wid}/users/${r.id}`}>编辑</Link>
                  </div>
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div style={{display:'flex', justifyContent:'flex-end', gap:8, marginTop:10, flexWrap:'wrap'}}>
          <button className="btn ghost" onClick={()=>setPage(p=>Math.max(1,p-1))} disabled={page<=1}>上一页</button>
          <div className="small-muted" style={{alignSelf:'center'}}>第 {page} / {totalPages} 页，共 {total} 条</div>
          <button className="btn ghost" onClick={()=>setPage(p=>Math.min(totalPages,p+1))} disabled={page>=totalPages}>下一页</button>
        </div>
      )}
    </div>
  )
}

function Th({ children, w }) {
  return <th style={{ textAlign:'left', padding:'10px 12px', fontWeight:700, width:w }}>{children}</th>
}
function Td({ children, mono }) {
  return <td style={{
    padding:'10px 12px', verticalAlign:'middle',
    fontFamily: mono ? 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace' : undefined
  }}>{children}</td>
}

