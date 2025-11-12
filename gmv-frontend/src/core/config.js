const DEFAULT_API_BASE = '/api/v1';

const rawBase = import.meta.env.VITE_API_BASE || DEFAULT_API_BASE;
const normalizedBase = String(rawBase).replace(/\/$/, '');

export const apiBase = normalizedBase;
export const apiRoot = apiBase;
