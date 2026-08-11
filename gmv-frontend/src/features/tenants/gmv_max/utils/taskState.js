export const ACTIVE_STATES = ["PENDING", "STARTED", "RETRY"];
export const TERMINAL_STATES = ["SUCCESS", "FAILURE", "REVOKED"];

export function resolveTaskState(raw) {
  const value = String(raw || "").toUpperCase();
  if (value === "SUCCEED" || value === "SUCCEEDED") return "SUCCESS";
  return value;
}

export function normalizeTaskState(value) {
  return resolveTaskState(value);
}

export function isActiveTaskState(state) {
  return Boolean(state) && ACTIVE_STATES.includes(normalizeTaskState(state));
}

export function isTerminalTaskState(state) {
  return Boolean(state) && TERMINAL_STATES.includes(normalizeTaskState(state));
}
