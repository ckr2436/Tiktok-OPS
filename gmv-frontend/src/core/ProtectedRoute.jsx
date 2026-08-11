// src/core/ProtectedRoute.jsx
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAppSelector } from '../app/hooks.js'

import { useSessionQuery } from '../features/platform/auth/hooks.js'

export default function ProtectedRoute() {
  const session = useAppSelector(s => s.session.data)
  const checked = useAppSelector(s => s.session.checked)
  const location = useLocation()

  useSessionQuery()

  // 探测中：不跳转，显示加载
  if (!checked) {
    return (
      <div style={{ display: 'grid', placeItems: 'center', height: '60vh' }}>
        <div className="card">加载中...</div>
      </div>
    )
  }

  // 明确未登录：带来源地址去登录
  if (!session) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }

  // 已登录：放行
  return <Outlet />
}

