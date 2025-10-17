export const apiBase = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')
export const apiPrefix = '/api/v1'
export const apiRoot = apiBase + apiPrefix
