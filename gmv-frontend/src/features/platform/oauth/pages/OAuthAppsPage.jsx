// src/features/platform/oauth/pages/OAuthAppsPage.jsx
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
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

  const queryClient = useQueryClient()
  const appsQuery = useQuery({
    queryKey: ['platform', 'oauth-apps'],
    queryFn: listProviderApps,
    staleTime: 5 * 60 * 1000,
  })
  const rows = appsQuery.data ?? []
  const loading = appsQuery.isLoading
  const errorMessage = appsQuery.error?.message || ''
  const empty = !loading && rows.length === 0

  const [showNew, setShowNew] = useState(false)
  const [editItem, setEditItem] = useState(null)

  const invalidateApps = () => queryClient.invalidateQueries({ queryKey: ['platform', 'oauth-apps'] })

  return (
    <div className="card card--elevated">
      <div style={{display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:12}}>
        <h3 style={{margin:0}}>OAuth Provider Apps（TikTok Business）</h3>
        <div style={{display:'flex', gap:8}}>
          <button className="btn ghost" onClick={() => appsQuery.refetch()} disabled={appsQuery.isFetching}>
            {appsQuery.isFetching ? 'Loading…' : 'Refresh'}
          </button>
          <button className="btn" onClick={()=>setShowNew(true)} disabled={!canCreate}>New App</button>
        </div>
      </div>

      <p className="small-muted" style={{marginTop:-4, marginBottom:12}}>
        仅平台管理员可见。表中不回显 Client Secret；<b>更新</b>时 Client Secret 留空代表不变。
      </p>

      {errorMessage && <div className="alert alert--error" style={{marginBottom:10}}>{errorMessage}</div>}

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

      <NewAppModal
        open={showNew}
        onClose={() => setShowNew(false)}
        onSaved={() => invalidateApps()}
        mode="create"
      />
      <NewAppModal
        open={!!editItem}
        onClose={() => setEditItem(null)}
        onSaved={() => {
          setEditItem(null)
          invalidateApps()
        }}
        mode="edit"
        initial={editItem}
      />
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

