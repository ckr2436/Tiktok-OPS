import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'

import auth from './service.js'
import { useAppDispatch } from '../../../app/hooks.js'
import { clearSession, setSession } from './sessionSlice.js'

const SESSION_QUERY_KEY = ['platform', 'session']
const ADMIN_EXISTS_QUERY_KEY = ['platform', 'admin-exists']

export function useSessionQuery(options = {}) {
  const dispatch = useAppDispatch()

  const query = useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: async () => {
      try {
        const session = await auth.session()
        return session?.id ? session : null
      } catch (error) {
        if (error?.status === 401) {
          return null
        }
        throw error
      }
    },
    staleTime: 5 * 60 * 1000,
    retry: false,
    refetchOnWindowFocus: false,
    ...options,
  })

  useEffect(() => {
    if (query.status === 'success') {
      if (query.data && query.data.id) {
        dispatch(setSession(query.data))
      } else {
        dispatch(clearSession())
      }
    } else if (query.status === 'error') {
      dispatch(clearSession())
    }
  }, [dispatch, query.data, query.status])

  return query
}

export function useAdminExistsQuery(options = {}) {
  return useQuery({
    queryKey: ADMIN_EXISTS_QUERY_KEY,
    queryFn: () => auth.adminExists(),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 5 * 60 * 1000,
    ...options,
  })
}
