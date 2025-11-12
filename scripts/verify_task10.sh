#!/usr/bin/env bash
set -euo pipefail

echo "== Task10: 验证 GMV Max 策略引擎 基本结构 =="

export PYTHONPATH="${PYTHONPATH:-}:backend"

python - <<'PY'
from app.data.models import ttb_gmvmax
assert hasattr(ttb_gmvmax, "TTBGmvMaxStrategyConfig")
from app.tasks import ttb_gmvmax_tasks as tasks
assert tasks.task_gmvmax_evaluate_strategy.name == "gmvmax.evaluate_strategy"
print("OK: model & task names")
PY

pytest -q backend/tests/test_gmvmax_strategy_import.py
pytest -q backend/tests/test_gmvmax_strategy_routes_smoke.py
echo "OK: Task10 基本测试通过"
