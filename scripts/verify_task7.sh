#!/usr/bin/env bash
set -euo pipefail

echo "== Task7: 验证 GMV Max 查询与日志端点 =="

test -f backend/app/features/tenants/ttb/gmvmax/router.py
grep -E '@router.get\("/campaigns/\{campaign_id\}/metrics"\)' -n backend/app/features/tenants/ttb/gmvmax/router.py >/dev/null
grep -E '@router.get\("/campaigns/\{campaign_id\}/actions"\)' -n backend/app/features/tenants/ttb/gmvmax/router.py >/dev/null

echo "OK: 路由函数存在"
