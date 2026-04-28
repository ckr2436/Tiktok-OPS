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

export default function CookiesTable({ items, loading, onToggle, onRefreshLogin }) {
  return (
    <div className="table-wrap" style={{ border: '1px solid var(--border)', borderRadius: 12 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 860 }}>
        <thead style={{ background: 'var(--panel-2)' }}>
          <tr>
            <Th w={120}>站点</Th>
            <Th w={200}>备注名</Th>
            <Th w={120}>状态</Th>
            <Th w={200}>最近保存时间</Th>
            <Th w={200}>过期时间</Th>
            <Th w={200}>更新时间</Th>
            <Th>操作</Th>
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <tr>
              <td colSpan={7} style={{ padding: 22 }}>
                加载中…
              </td>
            </tr>
          ) : items.length === 0 ? (
            <tr>
              <td colSpan={7} style={{ padding: 18, color: 'var(--muted)' }}>
                暂无数据
              </td>
            </tr>
          ) : (
            items.map((item) => (
              <tr key={item.id} style={{ borderTop: '1px solid var(--border)' }}>
                <Td>{siteLabel(item.site)}</Td>
                <Td>{item.label || <span className="small-muted">（未设置）</span>}</Td>
                <Td>
                  <ActiveBadge active={!!item.is_active} />
                </Td>
                <Td>{item.last_login_at ? new Date(item.last_login_at).toLocaleString() : '—'}</Td>
                <Td>{item.expires_at ? new Date(item.expires_at).toLocaleString() : '—'}</Td>
                <Td>{item.updated_at ? new Date(item.updated_at).toLocaleString() : '—'}</Td>
                <Td>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <button className="btn ghost" onClick={() => onRefreshLogin?.(item)}>
                      更新 Cookies
                    </button>
                    <button
                      className="btn ghost"
                      onClick={() => onToggle?.(item, !item.is_active)}
                      style={{ minWidth: 88 }}
                    >
                      {item.is_active ? '禁用' : '启用'}
                    </button>
                  </div>
                </Td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
