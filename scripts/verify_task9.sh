#!/usr/bin/env bash
set -euo pipefail

echo "== Task9: 验证 GMV Max 调度任务注册 =="

export PYTHONPATH="${PYTHONPATH:-}:backend"

python - <<'PY'
from app.services import scheduler_catalog as sc
catalog = {spec.name: spec for spec in sc.CATALOG}
assert any(name.startswith("gmvmax:") for name in catalog), "gmvmax tasks missing"
print("OK: scheduler catalog includes gmvmax entries")
PY

pytest -q backend/tests/test_gmvmax_scheduler_catalog.py
echo "OK: test_gmvmax_scheduler_catalog 通过"
