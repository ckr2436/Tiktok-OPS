// src/features/platform/admin/pages/AdminList.jsx
import { useEffect, useMemo, useState } from 'react'
import { useAppSelector } from '../../../../app/hooks'
import {
  listPlatformAdmins,
  deletePlatformAdmin,
  updatePlatformAdminDisplayName,
} from '../../admin/service'

export default function AdminList() {
  const me = useAppSelector(s => s.session?.data)
  const isOwner = (me?.role || '').toLowerCase() === 'owner' && me?.isPlatformAdmin

  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [size] = useState(20)

  const [loading, setLoading] = useState(false)
  const [rows, setRows] = useState([])
  const [total, setTotal] = useState(0)
  const [errMsg, setErrMsg] = useState('')

  const totalPages = useMemo(() => Math.max(1, Math.ceil((total || 0) / size)), [total, size])

  async function load() {
    setLoading(true)
    setErrMsg('')
    try {
      const data = await listPlatformAdmins({ q, page, size })
      setRows(data.items || [])
      setTotal(data.total || 0)
    } catch (e) {
      setErrMsg(e?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [q, page, size])

  async function onDelete(r) {
    if (!isOwner) return
    if (!confirm(`确定删除平台管理员「${r.username} / ${r.email}」吗？`)) return
    try {
      await deletePlatformAdmin(r.id)
      setRows(prev => prev.filter(x => x.id !== r.id))
      setTotal(t => Math.max(0, t - 1))
    } catch (e) {
      setErrMsg(e?.response?.data?.detail || e?.message || '删除失败')
    }
  }

  async function onRename(r) {
    const val = prompt('请输入新的 Display Name（留空表示清除）：', r.display_name || '')
    if (val === null) return
    try {
      await updatePlatformAdminDisplayName(r.id, val || null)
      setRows(prev => prev.map(x => x.id === r.id ? { ...x, display_name: val || null } : x))
    } catch (e) {
      const status = e?.response?.status
      const msg = (status === 404 || status === 405)
        ? '后端暂未实现 PATCH/PUT 接口：/api/v1/platform/admin/admins/{user_id}'
        : (e?.message || '修改失败')
      alert(msg)
    }
  }

  return (
    <div className="card card--elevated">
      <div style={{display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:12}}>
        <h3 style={{margin:0}}>平台管理员</h3>
        <div style={{display:'flex', gap:8}}>
          <input
            className="input"
            placeholder="搜索 用户名 / 邮箱 / 用户码"
            value={q}
            onChange={e => { setPage(1); setQ(e.target.value.trim()) }}
            style={{width:260}}
          />
        </div>
      </div>

      {errMsg && <div className="alert alert--error" style={{marginBottom:10}}>{errMsg}</div>}

      {/* ❗ 包上 .table-wrap，移除 overflow:hidden */}
      <div className="table-wrap" style={{border:'1px solid var(--border)', borderRadius:12}}>
        <table style={{width:'100%', borderCollapse:'collapse', minWidth: 720}}>
          <thead style={{background:'var(--panel-2)'}}>
            <tr>
              <Th w={80}>ID</Th>
              <Th w={140}>用户码</Th>
              <Th w={180}>用户名</Th>
              <Th w={260}>邮箱</Th>
              <Th w={200}>Display Name</Th>
              <Th w={110}>角色</Th>
              <Th>操作</Th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} style={{padding:22}}>加载中…</td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={7} style={{padding:18, color:'var(--muted)'}}>暂无数据</td></tr>
            ) : rows.map(r => (
              <tr key={r.id} style={{borderTop:'1px solid var(--border)'}}>
                <Td>{r.id}</Td>
                <Td mono>{r.usercode}</Td>
                <Td>{r.username}</Td>
                <Td>{r.email}</Td>
                <Td>{r.display_name || <span className="small-muted">（未设置）</span>}</Td>
                <Td><RoleBadge role={r.role} /></Td>
                <Td>
                  <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
                    <button className="btn ghost" onClick={() => onRename(r)}>修改名称</button>
                    {isOwner && r.role !== 'owner' && (
                      <button className="btn" style={{background:'#ef4444'}} onClick={() => onDelete(r)}>删除</button>
                    )}
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

function Th({children, w}) {
  return <th style={{textAlign:'left', padding:'10px 12px', fontWeight:700, width:w}}>{children}</th>
}
function Td({children, mono}) {
  return <td style={{padding:'10px 12px', fontFamily: mono ? 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace' : undefined}}>{children}</td>
}
function RoleBadge({role}) {
  const isOwner = (role || '').toLowerCase() === 'owner'
  const bg = isOwner ? '#f97316' : '#2563eb'
  return (
    <span style={{display:'inline-block', padding:'2px 8px', borderRadius:999, background:bg, color:'#fff', fontSize:12}}>
      {role}
    </span>
  )
}

