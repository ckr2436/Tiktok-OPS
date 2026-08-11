#!/usr/bin/env bash
set -euo pipefail

# Hermes async deployment smoke check.
#
# Usage:
#   bash backend/scripts/hermes_async_smoke_check.sh
#   API_BASE="http://127.0.0.1:8000/api/v1" bash backend/scripts/hermes_async_smoke_check.sh

API_BASE="${API_BASE:-http://127.0.0.1:8000/api/v1}"
SYSTEMD_SERVICE="${SYSTEMD_SERVICE:-gmv-celery-hermes.service}"
QUEUE_NAME="${QUEUE_NAME:-gmv.tasks.hermes_agent}"

echo "== Hermes async smoke check =="
echo "API_BASE=${API_BASE}"
echo "SYSTEMD_SERVICE=${SYSTEMD_SERVICE}"
echo "QUEUE_NAME=${QUEUE_NAME}"
echo

echo "[1/5] systemd worker status"
if systemctl is-active --quiet "${SYSTEMD_SERVICE}"; then
  echo "OK: ${SYSTEMD_SERVICE} is active"
else
  echo "FAIL: ${SYSTEMD_SERVICE} is not active"
  systemctl status "${SYSTEMD_SERVICE}" --no-pager || true
  exit 1
fi
echo

echo "[2/5] celery inspect ping"
if command -v celery >/dev/null 2>&1; then
  if celery -A app.celery_app.celery_app inspect ping >/tmp/hermes_celery_ping.out 2>&1; then
    cat /tmp/hermes_celery_ping.out
  else
    echo "WARN: celery inspect ping failed (maybe command not run in backend venv/cwd)"
    cat /tmp/hermes_celery_ping.out || true
  fi
else
  echo "WARN: celery command not found in PATH"
fi
echo

echo "[3/5] celery active queues"
if command -v celery >/dev/null 2>&1; then
  if celery -A app.celery_app.celery_app inspect active_queues >/tmp/hermes_celery_queues.out 2>&1; then
    cat /tmp/hermes_celery_queues.out
    if grep -q "${QUEUE_NAME}" /tmp/hermes_celery_queues.out; then
      echo "OK: queue ${QUEUE_NAME} appears in active_queues"
    else
      echo "WARN: queue ${QUEUE_NAME} not found in active_queues output"
    fi
  else
    echo "WARN: celery inspect active_queues failed"
    cat /tmp/hermes_celery_queues.out || true
  fi
else
  echo "WARN: celery command not found in PATH"
fi
echo

echo "[4/5] backend hermes health endpoint"
if curl -fsS "${API_BASE}/healthz" >/tmp/hermes_healthz.out 2>&1; then
  echo "OK: backend healthz reachable"
  cat /tmp/hermes_healthz.out
else
  echo "WARN: backend /healthz not reachable via ${API_BASE}/healthz"
  cat /tmp/hermes_healthz.out || true
fi
echo

echo "[5/5] recent hermes logs"
journalctl -u "${SYSTEMD_SERVICE}" -n 50 --no-pager || true

echo
echo "Done."
