import { Navigate, Outlet } from 'react-router-dom'
import { useAppSelector } from '../app/hooks.js'
import { parseBoolLike } from '../utils/booleans.js'

export default function AdminOnly(){
  const session = useAppSelector(s => s.session.data)
  const adminFlag = session?.isPlatformAdmin ?? session?.is_platform_admin
  const isPlatformAdmin = parseBoolLike(adminFlag)

  if(!session){ return <Navigate to="/login" replace /> }
  if(!isPlatformAdmin){ return <Navigate to="/dashboard" replace /> }
  return <Outlet />
}

