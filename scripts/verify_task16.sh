#!/usr/bin/env bash
set -euo pipefail
echo "=== Task16 Verify ==="

# 0) env
python -V || true
pip -V || true
pip install -q "pytest>=7" "fastapi" "httpx" "pydantic>=2" || true

# 1) FE: ban /gmvmax/campaigns
if grep -RIn --exclude-dir=node_modules "/gmvmax/campaigns" gmv-frontend/src >/dev/null; then
  echo "FAIL: Found deprecated '/gmvmax/campaigns' in FE"
  grep -RIn --exclude-dir=node_modules "/gmvmax/campaigns" gmv-frontend/src
  exit 1
else
  echo "OK: FE has no '/gmvmax/campaigns' references"
fi

# 2) BE: no GET body (static check)
if rg -n "def [^\\n]*get_[^\\n]*BaseModel" backend/app/features/tenants/ttb/gmv_max >/dev/null; then
  echo "WARN: possible GET with Body signature, please re-check"
fi

# 3) run BE smoke tests
pytest -q backend/tests/test_gmvmax_contract_smoke.py || exit 1
echo "OK: backend smoke passed"

echo "=== Task16 Verify OK ==="
