export function formatError(error) {
  if (!error) return null;
  if (typeof error === 'string') return error;
  if (error?.response?.data?.error?.message) return error.response.data.error.message;
  if (error?.response?.data?.message) return error.response.data.message;
  if (error?.response?.data?.detail) {
    const { detail } = error.response.data;
    if (typeof detail === 'string') return detail;
    try {
      return JSON.stringify(detail);
    } catch (serializationError) {
      return String(detail);
    }
  }
  if (error?.message) return error.message;
  return 'Request failed';
}

export function isSyncRateLimitedError(error) {
  if (!error) return false;
  const status = error?.response?.status ?? error?.status;
  const code = String(
    error?.response?.data?.error?.code || error?.payload?.error?.code || error?.code || '',
  ).toUpperCase();
  const message = String(formatError(error) || '').toLowerCase();
  return (
    status === 429 ||
    code === 'SYNC_RATE_LIMITED' ||
    message.includes('sync was triggered too recently') ||
    message.includes('同步任务触发过于频繁')
  );
}
