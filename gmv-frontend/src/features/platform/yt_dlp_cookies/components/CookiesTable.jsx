import { siteLabel } from '../api.js'

function Th({ children, w }) {
  return (
    <th style={{ textAlign: 'left', padding: '10px 12px', fontWeight: 700, width: w }}>
      {children}
    </th>
  )
}

function Td({ children, mono }) {
  return (
    <td
      style={{
        padding: '10px 12px',
        fontFamily: mono
          ? 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace'
          : undefined,
      }}
    >
      {children}
    </td>
  )
}

function ActiveBadge({ active }) {
  const bg = active ? '#16a34a' : '#9ca3af'
  const text = active ? '启用' : '已禁用'
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '2px 8px',
        borderRadius: 999,
        background: bg,
        color: '#fff',
        fontSize: 12,
      }}
    >
      {text}
    </span>
  )
}

function HealthBadge({ item }) {
  const status = item.reauth_required ? 'reauth_required' : item.health_status
  const labels = { healthy: '健康', refreshing: '保活中', reauth_required: '需重新登录', unknown: '待验证' }
  const colors = { healthy: '#16a34a', refreshing: '#2563eb', reauth_required: '#dc2626', unknown: '#6b7280' }
  return <span style={{ display: 'inline-block', padding: '2px 8px', borderRadius: 999, color: '#fff', background: colors[status] || colors.unknown, fontSize: 12 }}>{labels[status] || status || '待验证'}</span>
}

export default function CookiesTable({ items, loading, onToggle, onRefreshLogin, onDelete, deletingId }) {
  return (
    <div className="table-wrap" style={{ border: '1px solid var(--border)', borderRadius: 12 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 940 }}>
        <thead style={{ background: 'var(--panel-2)' }}>
          <tr>
            <Th w={120}>站点</Th>
            <Th w={200}>备注名</Th>
            <Th w={150}>状态</Th>
            <Th w={220}>自动保活</Th>
            <Th w={200}>最近保存时间</Th>
            <Th w={200}>过期时间</Th>
            <Th w={200}>更新时间</Th>
            <Th>操作</Th>
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <tr>
              <td colSpan={8} style={{ padding: 22 }}>
                加载中…
              </td>
            </tr>
          ) : items.length === 0 ? (
            <tr>
              <td colSpan={8} style={{ padding: 18, color: 'var(--muted)' }}>
                暂无数据
              </td>
            </tr>
          ) : (
            items.map((item) => {
              const deleting = deletingId === item.id
              return (
                <tr key={item.id} style={{ borderTop: '1px solid var(--border)' }}>
                  <Td>{siteLabel(item.site)}</Td>
                  <Td>{item.label || <span className="small-muted">（未设置）</span>}</Td>
                  <Td>
                    <ActiveBadge active={!!item.is_active} />
                    <div style={{ marginTop: 6 }}><HealthBadge item={item} /></div>
                  </Td>
                  <Td>
                    <div>{item.last_verified_at ? `上次验证：${new Date(item.last_verified_at).toLocaleString()}` : '尚未自动验证'}</div>
                    <div className="small-muted">{item.next_keepalive_at ? `下次：${new Date(item.next_keepalive_at).toLocaleString()}` : '等待安排'}</div>
                    {item.keepalive_error && <div style={{ color: '#dc2626' }}>{item.keepalive_error}</div>}
                  </Td>
                  <Td>{item.last_login_at ? new Date(item.last_login_at).toLocaleString() : '—'}</Td>
                  <Td>{item.expires_at ? new Date(item.expires_at).toLocaleString() : '—'}</Td>
                  <Td>{item.updated_at ? new Date(item.updated_at).toLocaleString() : '—'}</Td>
                  <Td>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                      <button className="btn ghost" onClick={() => onRefreshLogin?.(item)} disabled={deleting}>
                        更新 Cookies
                      </button>
                      <button
                        className="btn ghost"
                        onClick={() => onToggle?.(item, !item.is_active)}
                        disabled={deleting}
                        style={{ minWidth: 88 }}
                      >
                        {item.is_active ? '禁用' : '启用'}
                      </button>
                      <button
                        className="btn ghost"
                        onClick={() => onDelete?.(item)}
                        disabled={deleting}
                        style={{
                          minWidth: 88,
                          borderColor: '#fecaca',
                          background: '#fff1f2',
                          color: '#dc2626',
                        }}
                      >
                        {deleting ? '删除中…' : '删除'}
                      </button>
                    </div>
                  </Td>
                </tr>
              )
            })
          )}
        </tbody>
      </table>
    </div>
  )
}
