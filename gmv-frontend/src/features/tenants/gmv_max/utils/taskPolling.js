import http from '@/lib/http.js';

const IDLE_STATES = ['PENDING', 'STARTED', 'RETRY'];

function normalizeState(value) {
  return String(value || '').toUpperCase();
}

/**
 * Poll GMV Max task status until it reaches a terminal state.
 *
 * @param {string} statusUrl Task status endpoint returned by sync API.
 * @param {object} [options]
 * @param {number} [options.intervalMs=2000] Polling interval in milliseconds.
 * @param {number} [options.timeoutMs=60000] Total timeout in milliseconds.
 * @param {(state: string) => void} [options.onProgress] Callback for each polled state.
 * @returns {Promise<object>} Latest task status response.
 */
export async function pollTaskStatus(statusUrl, options = {}) {
  const { intervalMs = 2000, timeoutMs = 60_000, onProgress } = options;
  const start = Date.now();

  // eslint-disable-next-line no-constant-condition
  while (true) {
    const response = await http.get(statusUrl);
    const data = response?.data ?? response ?? {};
    const state = normalizeState(data.state || data.status);

    onProgress?.(state);

    if (!IDLE_STATES.includes(state)) {
      return { ...data, state };
    }

    if (Date.now() - start > timeoutMs) {
      throw new Error('Task polling timeout');
    }

    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

export function isTaskInProgress(state) {
  return IDLE_STATES.includes(normalizeState(state));
}

export function isTaskSuccessful(state) {
  return normalizeState(state) === 'SUCCESS';
}
