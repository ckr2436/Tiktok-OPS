// src/features/platform/tenants/pages/TenantList.jsx
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppSelector } from '../../../../app/hooks'
import { listCompanies, deleteCompany } from '../service'

export default function TenantList() {
  const navigate = useNavigate()
  const me = useAppSelector(s => s.session?.data)
  const isPlatformOwner = (me?.role || '').toLowerCase() === 'owner' && me?.isPlatformAdmin

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
      const data = await listCompanies({ q, page, size })
      setRows(data.items || [])
      setTotal(data.total || 0)
    } catch (e) {
      setErrMsg(e?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [q, page, size])

  async function onDelete(ws) {
    if (!isPlatformOwner) return
    if (ws.company_code === '0000') return
    if (!confirm(`确定删除公司「${ws.name} / ${ws.company_code}」吗？将同时软删其所有用户。`)) return
    try {
      await deleteCompany(ws.id)
      setRows(prev => prev.filter(x => x.id !== ws.id))
      setTotal(t => Math.max(0, t - 1))
    } catch (e) {
      alert(e?.response?.data?.detail || e?.message || '删除失败')
    }
  }

  return (
    <div className="card card--elevated">
      <div style={{display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:12}}>
        <h3 style={{margin:0}}>公司列表</h3>
        <div style={{display:'flex', gap:8}}>
          <input
            className="input"
            placeholder="搜索 公司名 / 公司码"
            value={q}
            onChange={e => { setPage(1); setQ(e.target.value.trim()) }}
            style={{width:260}}
          />
          <button className="btn" onClick={() => navigate('/platform/tenants/create')}>新建公司</button>
        </div>
      </div>

      {errMsg && <div className="alert alert--error" style={{marginBottom:10}}>{errMsg}</div>}

      {/* ❗ 改为 .table-wrap */}
      <div className="table-wrap" style={{border:'1px solid var(--border)', borderRadius:12}}>
        <table style={{width:'100%', borderCollapse:'collapse', minWidth: 700}}>
          <thead style={{background:'var(--panel-2)'}}>
            <tr>
              <Th w={90}>ID</Th>
              <Th w={120}>公司码</Th>
              <Th>公司名</Th>
              <Th w={120}>成员数</Th>
              <Th w={280}>Owner 邮箱</Th>
              <Th w={200}>操作</Th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={6} style={{padding:22}}>加载中…</td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={6} style={{padding:18, color:'var(--muted)'}}>暂无数据</td></tr>
            ) : rows.map(ws => (
              <tr key={ws.id} style={{borderTop:'1px solid var(--border)'}}>
                <Td>{ws.id}</Td>
                <Td mono>{ws.company_code}</Td>
                <Td>{ws.name}</Td>
                <Td>{ws.members}</Td>
                <Td>{ws.owner_email || <span className="small-muted">（未设置）</span>}</Td>
                <Td>
                  <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
                    {isPlatformOwner && ws.company_code !== '0000' && (
                      <button className="btn" style={{background:'#ef4444'}} onClick={() => onDelete(ws)}>删除</button>
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

