#!/usr/bin/env bash
set -Eeuo pipefail

QUEUE="${1:?Usage: celery-worker.sh <queue-name>}"
CELERY_BIN="/opt/gmv/python3.13/bin/celery"
HOSTNAME="$(hostname)-${QUEUE}"

# The Python task registry uses this role only to avoid importing unrelated
# worker task graphs. The queue and all resource limits remain deployment
# configuration.
export GMV_CELERY_WORKER_QUEUE="$QUEUE"

QUEUE_KEY="$(echo "$QUEUE" | tr '.-' '__' | tr '[:lower:]' '[:upper:]')"

get_queue_env() {
  local base_name="$1"
  local default_value="$2"
  local queue_var="${base_name}_${QUEUE_KEY}"

  if [[ -n "${!queue_var:-}" ]]; then
    printf '%s' "${!queue_var}"
  elif [[ -n "${!base_name:-}" ]]; then
    printf '%s' "${!base_name}"
  else
    printf '%s' "$default_value"
  fi
}

LOGLEVEL="$(get_queue_env CELERY_LOGLEVEL INFO)"
CONCURRENCY="$(get_queue_env CELERY_WORKER_CONCURRENCY 4)"
PREFETCH="$(get_queue_env CELERY_WORKER_PREFETCH 1)"
POOL="$(get_queue_env CELERY_POOL prefork)"
MAX_TASKS="$(get_queue_env CELERY_MAX_TASKS_PER_CHILD 500)"
MAX_MEM="$(get_queue_env CELERY_MAX_MEMORY_PER_CHILD 300000)"

STATE_DIR="/var/lib/gmv"
RUN_DIR="/run/gmv"
PIDFILE="${RUN_DIR}/celery-worker@${QUEUE}.pid"
STATEDB="${STATE_DIR}/celery-worker@${QUEUE}.state"

mkdir -p "$STATE_DIR" "$RUN_DIR"

exec "$CELERY_BIN" \
  -A app.celery_app.celery_app worker \
  --hostname="$HOSTNAME" \
  --loglevel="$LOGLEVEL" \
  --concurrency="$CONCURRENCY" \
  --prefetch-multiplier="$PREFETCH" \
  -P "$POOL" \
  --statedb="$STATEDB" \
  -Q "$QUEUE" \
  --pidfile="$PIDFILE" \
  --max-tasks-per-child="$MAX_TASKS" \
  --max-memory-per-child="$MAX_MEM" \
  -Ofair \
  --without-mingle \
  --without-gossip \
  --without-heartbeat
