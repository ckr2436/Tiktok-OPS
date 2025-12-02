export const ACTIVE_STATES = ["PENDING", "STARTED"];
export const TERMINAL_STATES = ["SUCCESS", "FAILURE", "REVOKED"];

export function normalizeTaskState(value) {
  return String(value || "").toUpperCase();
}

export function isActiveTaskState(state) {
  return Boolean(state) && ACTIVE_STATES.includes(normalizeTaskState(state));
}

export function isTerminalTaskState(state) {
  return Boolean(state) && TERMINAL_STATES.includes(normalizeTaskState(state));
}
