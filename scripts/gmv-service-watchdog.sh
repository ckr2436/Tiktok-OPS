#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -e /run/gmv/maintenance || -e /etc/gmv/maintenance ]]; then
  exit 0
fi

SERVICE_LIST="${GMV_REQUIRED_SERVICES_FILE:-/etc/gmv/required-services.conf}"
if [[ ! -r "$SERVICE_LIST" ]]; then
  /usr/bin/logger -t gmv-service-watchdog \
    "required service list is unavailable: $SERVICE_LIST"
  exit 1
fi

mapfile -t units < <(
  /usr/bin/sed -e 's/[[:space:]]*#.*$//' -e '/^[[:space:]]*$/d' "$SERVICE_LIST"
)
if (( ${#units[@]} == 0 )); then
  /usr/bin/logger -t gmv-service-watchdog \
    "required service list is empty: $SERVICE_LIST"
  exit 1
fi

failed=0
for unit in "${units[@]}"; do
  if ! /usr/bin/systemctl is-active --quiet "$unit"; then
    /usr/bin/logger -t gmv-service-watchdog "recovering inactive unit: $unit"
    if ! /usr/bin/systemctl start "$unit"; then
      /usr/bin/logger -t gmv-service-watchdog "failed to recover unit: $unit"
      failed=1
    fi
  fi
done

exit "$failed"
