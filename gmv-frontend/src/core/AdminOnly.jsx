import { Navigate, Outlet } from 'react-router-dom'
import { useAppSelector } from '../app/hooks.js'

export default function AdminOnly(){
  const session = useAppSelector(s => s.session.data)
  if(!session){ return <Navigate to="/login" replace /> }
  if(!session.isPlatformAdmin){ return <Navigate to="/dashboard" replace /> }
  return <Outlet />
}

