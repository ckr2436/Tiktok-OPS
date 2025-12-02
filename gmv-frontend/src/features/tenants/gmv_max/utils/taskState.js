export const ACTIVE_STATES = ["PENDING", "STARTED", "RETRY"];
export const TERMINAL_STATES = ["SUCCESS", "FAILURE", "REVOKED"];

export function isActiveTaskState(state) {
  return Boolean(state) && ACTIVE_STATES.includes(String(state).toUpperCase());
}

export function isTerminalTaskState(state) {
  return Boolean(state) && TERMINAL_STATES.includes(String(state).toUpperCase());
}
