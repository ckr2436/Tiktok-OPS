#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   sudo bash backend/scripts/install_hermes_celery_systemd.sh \
#     /opt/gmv/GMV-OPS/backend www-data www-data
#
# Args:
#   1) backend root path (contains app/, .venv/, .env)
#   2) service user (default: www-data)
#   3) service group (default: www-data)

BACKEND_DIR="${1:-/opt/gmv/GMV-OPS/backend}"
SERVICE_USER="${2:-www-data}"
SERVICE_GROUP="${3:-www-data}"
SERVICE_NAME="gmv-celery-hermes.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

if [[ ! -d "${BACKEND_DIR}" ]]; then
  echo "ERROR: backend dir not found: ${BACKEND_DIR}" >&2
  exit 1
fi

if [[ ! -x "${BACKEND_DIR}/.venv/bin/celery" ]]; then
  echo "ERROR: celery binary not found: ${BACKEND_DIR}/.venv/bin/celery" >&2
  exit 1
fi

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=GMV Celery Worker (Hermes queue)
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${BACKEND_DIR}
EnvironmentFile=${BACKEND_DIR}/.env
ExecStart=${BACKEND_DIR}/.venv/bin/celery -A app.celery_app.celery_app worker -Q gmv.tasks.hermes_agent -n hermes@%%H --loglevel=INFO --concurrency=2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl status "${SERVICE_NAME}" --no-pager

echo
echo "Installed and started ${SERVICE_NAME}"
echo "Check logs with:"
echo "  journalctl -u ${SERVICE_NAME} -f"
