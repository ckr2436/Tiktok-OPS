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
