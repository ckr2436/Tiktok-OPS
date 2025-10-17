// src/features/platform/oauth/pages/OAuthAppsPage.jsx
import { useEffect, useMemo, useState } from 'react'
import { useAppSelector } from '../../../../app/hooks.js'
import { parseBoolLike } from '../../../../utils/booleans.js'
import { listProviderApps } from '../../oauth/service.js'
import NewAppModal from '../components/NewAppModal.jsx'

export default function OAuthAppsPage() {
  const me = useAppSelector(s => s.session?.data)
  const adminFlag = me?.isPlatformAdmin ?? me?.is_platform_admin
  const isPlatformAdmin = parseBoolLike(adminFlag)
  const isOwner = (me?.role || '').toLowerCase() === 'owner'
  const canCreate = isPlatformAdmin && isOwner

  const [loading, setLoading] = useState(false)
  const [rows, setRows] = useState([])
  const [err, setErr] = useState('')

  const [showNew, setShowNew] = useState(false)
  const [editItem, setEditItem] = useState(null)

  async function load() {
    setLoading(true); setErr('')
    try {
      const items = await listProviderApps()
      setRows(Array.isArray(items) ? items : (items?.items ?? []))
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [])
  const empty = !loading && (!rows || rows.length === 0)

  return (
    <div className="card card--elevated">
      <div style={{display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:12}}>
        <h3 style={{margin:0}}>OAuth Provider Apps（TikTok Business）</h3>
        <div style={{display:'flex', gap:8}}>
          <button className="btn ghost" onClick={load} disabled={loading}>{loading ? 'Loading…' : 'Refresh'}</button>
          <button className="btn" onClick={()=>setShowNew(true)} disabled={!canCreate}>New App</button>
        </div>
      </div>

      <p className="small-muted" style={{marginTop:-4, marginBottom:12}}>
        仅平台管理员可见。表中不回显 Client Secret；<b>更新</b>时 Client Secret 留空代表不变。
      </p>

      {err && <div className="alert alert--error" style={{marginBottom:10}}>{err}</div>}

      {/* ❗ table-wrap 开启横向滚动 */}
      <div className="table-wrap" style={{border:'1px solid var(--border)', borderRadius:12}}>
        <table style={{width:'100%', borderCollapse:'collapse', minWidth: 900}}>
          <thead style={{background:'var(--panel-2)'}}>
            <tr>
              <Th w={80}>ID</Th>
              <Th w={240}>Name</Th>
              <Th w={260}>Client ID (App ID)</Th>
              <Th w={380}>Redirect URI</Th>
              <Th w={90}>Enabled</Th>
              <Th w={120}>Key Version</Th>
              <Th w={200}>Updated</Th>
              <Th w={160}>操作</Th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={8} style={{padding:22}}>加载中…</td></tr>
            ) : empty ? (
              <tr><td colSpan={8} style={{padding:18, color:'var(--muted)'}}>尚未配置 Provider App</td></tr>
            ) : rows.map(r => (
              <tr key={r.id} style={{borderTop:'1px solid var(--border)'}}>
                <Td>{r.id}</Td>
                <Td className="truncate">{r.name}</Td>
                <Td mono title={r.client_id}>{r.client_id}</Td>
                <Td className="truncate" title={r.redirect_uri}>{r.redirect_uri}</Td>
                <Td>{r.is_enabled ? 'Yes' : 'No'}</Td>
                <Td>{r.client_secret_key_version}</Td>
                <Td>{r.updated_at || '-'}</Td>
                <Td>
                  <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
                    <button
                      className="btn small"
                      disabled={!canCreate}
                      onClick={() => setEditItem(r)}
                      title={!canCreate ? '需要平台 Owner 权限' : '编辑该应用'}
                    >
                      Edit
                    </button>
                  </div>
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <NewAppModal open={showNew} onClose={() => setShowNew(false)} onSaved={() => load()} mode="create" />
      <NewAppModal open={!!editItem} onClose={() => setEditItem(null)} onSaved={() => { setEditItem(null); load() }} mode="edit" initial={editItem} />
    </div>
  )
}

function Th({children, w}) {
  return <th style={{textAlign:'left', padding:'10px 12px', fontWeight:700, width:w}}>{children}</th>
}
function Td({children, mono, className, title}) {
  return (
    <td
      className={className}
      title={title}
      style={{
        padding:'10px 12px',
        maxWidth: 460,
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        fontFamily: mono ? 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace' : undefined
      }}
    >
      {children}
    </td>
  )
}

