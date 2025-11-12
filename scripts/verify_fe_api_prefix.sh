#!/usr/bin/env bash
set -euo pipefail

if [ ! -d gmv-frontend/dist ]; then
  echo "dist not found; run build first"
  exit 1
fi

if grep -R "/api/v1/api/v1" gmv-frontend/dist >/dev/null; then
  echo "FAIL: duplicated '/api/v1' found in built assets"
  exit 1
fi

echo "OK: no duplicated '/api/v1' in FE build"
