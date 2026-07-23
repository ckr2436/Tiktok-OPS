import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { useAppSelector } from '../../../../app/hooks.js'
import { parseBoolLike } from '../../../../utils/booleans.js'
import { deleteProviderApp, listProviderApps } from '../../oauth/service.js'
import NewAppModal from '../components/NewAppModal.jsx'


function providerLabel(value) {
  return value === 'tiktok_shop' ? 'TikTok Shop' : 'TikTok Business'
}


export default function OAuthAppsPage() {
  const me = useAppSelector((state) => state.session?.data)
  const isPlatformAdmin = parseBoolLike(me?.isPlatformAdmin ?? me?.is_platform_admin)
  const canManage = isPlatformAdmin && String(me?.role || '').toLowerCase() === 'owner'
  const queryClient = useQueryClient()
  const [showNew, setShowNew] = useState(false)
  const [editItem, setEditItem] = useState(null)
  const [deletingId, setDeletingId] = useState(null)
  const [error, setError] = useState('')

  const appsQuery = useQuery({
    queryKey: ['platform', 'oauth-apps'],
    queryFn: listProviderApps,
    staleTime: 5 * 60 * 1000,
  })
  const rows = appsQuery.data || []
  const reload = () => queryClient.invalidateQueries({ queryKey: ['platform', 'oauth-apps'] })

  async function removeApp(item) {
    if (!item?.id || !window.confirm(`确定删除应用“${item.name}”吗？此操作不可撤销。`)) return
    setError('')
    setDeletingId(item.id)
    try {
      await deleteProviderApp(item.id)
      await reload()
    } catch (requestError) {
      setError(requestError?.response?.data?.detail?.message || requestError?.response?.data?.detail || requestError?.message || '删除失败。')
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div style={{ display: 'grid', gap: 18 }}>
      <header style={{ display: 'flex', justifyContent: 'space-between', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24 }}>OAuth 应用</h1>
          <p className="small-muted" style={{ margin: '6px 0 0' }}>
            分别管理 TikTok Business 与 TikTok Shop 凭证。Client Secret 仅加密保存，不会回显。
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn ghost" onClick={() => appsQuery.refetch()} disabled={appsQuery.isFetching}>
            {appsQuery.isFetching ? '刷新中...' : '刷新'}
          </button>
          <button className="btn" onClick={() => setShowNew(true)} disabled={!canManage}>新建应用</button>
        </div>
      </header>

      {!canManage && <div className="alert">只有平台 Owner 可以创建、更新或删除 OAuth 应用。</div>}
      {(error || appsQuery.error) && (
        <div className="alert alert--error">{error || appsQuery.error?.message || '加载失败。'}</div>
      )}

      <section className="card">
        <div className="table-wrap">
          <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 1080 }}>
            <thead style={{ background: 'var(--panel-2)' }}>
              <tr>
                <Th>类型</Th>
                <Th>名称</Th>
                <Th>App ID / App Key</Th>
                <Th>Service ID</Th>
                <Th>Redirect URI</Th>
                <Th>状态</Th>
                <Th>更新时间</Th>
                <Th>操作</Th>
              </tr>
            </thead>
            <tbody>
              {appsQuery.isLoading ? (
                <tr><td colSpan={8} style={{ padding: 24 }}>正在加载...</td></tr>
              ) : rows.length === 0 ? (
                <tr><td colSpan={8} style={{ padding: 24, color: 'var(--muted)' }}>尚未配置 OAuth 应用。</td></tr>
              ) : rows.map((item) => (
                <tr key={item.id} style={{ borderTop: '1px solid var(--border)' }}>
                  <Td>{providerLabel(item.provider)}</Td>
                  <Td><strong>{item.name}</strong></Td>
                  <Td mono>{item.client_id}</Td>
                  <Td mono>{item.service_id || '-'}</Td>
                  <Td title={item.redirect_uri}>{item.redirect_uri}</Td>
                  <Td>{item.is_enabled ? '启用' : '停用'}</Td>
                  <Td>{item.updated_at || '-'}</Td>
                  <Td>
                    <div style={{ display: 'flex', gap: 8 }}>
                      <button className="btn sm ghost" disabled={!canManage} onClick={() => setEditItem(item)}>编辑</button>
                      <button className="btn sm danger" disabled={!canManage || deletingId === item.id} onClick={() => removeApp(item)}>
                        {deletingId === item.id ? '删除中...' : '删除'}
                      </button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <NewAppModal open={showNew} onClose={() => setShowNew(false)} onSaved={reload} mode="create" />
      <NewAppModal
        open={Boolean(editItem)}
        onClose={() => setEditItem(null)}
        onSaved={async () => {
          setEditItem(null)
          await reload()
        }}
        mode="edit"
        initial={editItem}
      />
    </div>
  )
}


function Th({ children }) {
  return <th style={{ textAlign: 'left', padding: '10px 12px', fontWeight: 700 }}>{children}</th>
}


function Td({ children, mono, title }) {
  return (
    <td title={title} style={{ padding: '11px 12px', maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontFamily: mono ? 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' : undefined }}>
      {children}
    </td>
  )
}

