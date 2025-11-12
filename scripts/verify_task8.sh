#!/usr/bin/env bash
set -euo pipefail

echo "==> Task8 自检开始：Celery 任务可导入、命名正确、关键单测通过"

export PYTHONPATH="${PYTHONPATH:-}:backend"

python - <<'PY'
import importlib
m = importlib.import_module('app.tasks.ttb_gmvmax_tasks')
assert m.task_gmvmax_sync_campaigns.name == 'gmvmax.sync_campaigns'
assert m.task_gmvmax_sync_metrics.name == 'gmvmax.sync_metrics'
assert m.task_gmvmax_apply_action.name == 'gmvmax.apply_action'
print("OK: import & names")
PY

pytest -q backend/tests/test_gmvmax_tasks_import.py
echo "OK: 关键单测通过"
