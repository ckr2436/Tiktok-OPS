// src/core/ProtectedRoute.jsx
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useEffect } from 'react'
import { useAppDispatch, useAppSelector } from '../app/hooks.js'

// 由守卫主动探测一次会话，避免刷新时先跳 /login
import auth from '../features/platform/auth/service.js'
import { setSession, markChecked } from '../features/platform/auth/sessionSlice.js'

export default function ProtectedRoute() {
  const dispatch = useAppDispatch()
  const session = useAppSelector(s => s.session.data)
  const checked = useAppSelector(s => s.session.checked)
  const location = useLocation()

  // 首次进入或刷新时探测会话
  useEffect(() => {
    let aborted = false
    ;(async () => {
      if (checked) return
      try {
        const s = await auth.session()
        if (!aborted && s?.id) dispatch(setSession(s))
      } catch {
        // 未登录/401：忽略，走未登录分支
      } finally {
        if (!aborted) dispatch(markChecked())
      }
    })()
    return () => { aborted = true }
  }, [checked, dispatch])

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

