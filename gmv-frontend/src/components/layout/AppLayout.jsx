// src/components/layout/AppLayout.jsx
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { useAppSelector } from '../../app/hooks.js'
import Header from './Header.jsx'
import Sidebar from './Sidebar.jsx'
import { buildMenus } from './menus.js'   // ★ 统一从 menus.js 生成菜单

export default function AppLayout({ children }) {
  const session = useAppSelector(s => s.session?.data)
  const location = useLocation()
  const navigate = useNavigate()
  const [flash, setFlash] = useState(null)

  useEffect(() => {
    if (location.state?.err) {
      setFlash(location.state.err)
      navigate(location.pathname + location.search, { replace: true, state: {} })
    }
  }, [location, navigate])

  useEffect(() => {
    if (!flash) return
    const timer = setTimeout(() => setFlash(null), 4000)
    return () => clearTimeout(timer)
  }, [flash])

  // 统一从单一来源构建菜单，避免手动漏项
  const groups = buildMenus(session || {})

  return (
    <div className="shell">
      <Header />

      <div className="layout">
        <Sidebar groups={groups} />
        <main className="content">
          {flash && (
            <div className="alert alert--error" role="alert" style={{ marginBottom: '16px' }}>
              {flash}
            </div>
          )}
          {children ?? <Outlet />}
        </main>
      </div>

      <footer className="footer">
        <span className="small-muted">© 2025 Drafyn · All rights reserved.</span>
        <span>·</span>
        <a href="/terms.html" target="_blank" rel="noopener">服务条款</a>
        <span>·</span>
        <a href="/privacy.html" target="_blank" rel="noopener">隐私政策</a>
      </footer>
    </div>
  )
}

