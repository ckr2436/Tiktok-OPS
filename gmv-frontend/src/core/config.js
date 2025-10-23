const metaEnv = (typeof import.meta !== 'undefined' && import.meta?.env) || {}
const base = metaEnv.VITE_API_BASE || process.env.VITE_API_BASE || ''
export const apiBase = String(base || '').replace(/\/$/, '')
export const apiPrefix = '/api/v1'
export const apiRoot = apiBase + apiPrefix
