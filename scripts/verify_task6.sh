#!/usr/bin/env bash
set -euo pipefail

echo "== Task6 验证：检查 gmvmax 子路由与装配 =="

test -f backend/app/features/tenants/ttb/gmvmax/router.py
grep -F 'prefix="/api/v1/tenants/{workspace_id}/ttb/gmvmax"' -n backend/app/features/tenants/ttb/gmvmax/router.py >/dev/null
grep -E '@router\.get\("/campaigns"\)' -n backend/app/features/tenants/ttb/gmvmax/router.py >/dev/null
grep -E '@router\.get\("/campaigns/\{campaign_id\}"\)' -n backend/app/features/tenants/ttb/gmvmax/router.py >/dev/null
grep -E '@router\.post\("/campaigns/\{campaign_id\}/metrics/sync"\)' -n backend/app/features/tenants/ttb/gmvmax/router.py >/dev/null
grep -E '@router\.post\("/campaigns/actions"\)' -n backend/app/features/tenants/ttb/gmvmax/router.py >/dev/null

grep -E 'include_router\(gmvmax_router' -n backend/app/features/tenants/ttb/router.py >/dev/null

echo "OK: gmvmax 路由结构通过"

pytest backend/tests/test_gmvmax_routes_smoke_newprefix.py
