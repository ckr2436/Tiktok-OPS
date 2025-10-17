// src/components/layout/Sidebar.jsx
import { useState } from 'react'
import { NavLink } from 'react-router-dom'

/**
 * Sidebar
 * props.groups: [
 *   { title: '平台', items: [
 *      { to:'/platform', label:'系统控制台', exact:true },
 *      { to:'/platform/tenants', label:'公司管理' },
 *      { to:'/platform/apis', label:'API 文档' },
 *      // 外链也支持：
 *      // { href:'/some.html', label:'外部页', external:true }
 *   ]}
 * ]
 */
export default function Sidebar({ groups = [] }) {
  return (
    <aside className="sidebar" aria-label="侧边导航">
      {groups.map((g, i) => (
        <SidebarGroup key={i} title={g.title} items={g.items || []} />
      ))}
    </aside>
  )
}

function SidebarGroup({ title, items }) {
  const [open, setOpen] = useState(true)
  return (
    <div className={'sidebar-group' + (open ? '' : ' collapsed')}>
      <button
        type="button"
        className="sidebar-group__title"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
      >
        <span className="sidebar-group__text">{title}</span>
        <svg className="sidebar-group__caret" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
          <path d="M8 10l4 4 4-4" stroke="currentColor" strokeWidth="2" />
        </svg>
      </button>

      <div className="sidebar-links">
        {items.map(it => {
          // 外链
          if (it.href) {
            return (
              <a
                key={it.href}
                className="sidebar-link"                // ← 修正：className 放到这里
                href={it.href}                           // ← 修正：href 使用真实地址
                target={it.external ? '_blank' : undefined}
                rel={it.external ? 'noopener' : undefined}
                title={it.label}
              >
                <i className="link-dot" />
                <span className="sidebar-link__label">{it.label}</span>
              </a>
            )
          }
          // 内部路由
          return (
            <NavLink
              key={it.to}
              to={it.to}
              end={!!it.exact}               // ★ 精确匹配：防止父级也被激活
              className={({ isActive }) => 'sidebar-link' + (isActive ? ' active' : '')}
              title={it.label}
            >
              <i className="link-dot" />
              <span className="sidebar-link__label">{it.label}</span>
            </NavLink>
          )
        })}
      </div>
    </div>
  )
}

