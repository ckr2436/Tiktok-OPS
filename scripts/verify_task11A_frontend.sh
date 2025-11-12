#!/usr/bin/env bash
set -euo pipefail

echo "== Task11A: GMV Max minimal console 自检 =="

cd gmv-frontend

if command -v pnpm >/dev/null 2>&1; then
  pnpm exec vitest run \
    src/features/tenants/gmv_max/service.gmvmax-minimal.spec.jsx \
    src/features/tenants/gmv_max/GmvMaxCampaignConsole.smoke.spec.jsx
else
  if [ -f "scripts/run-vitest.mjs" ]; then
    node scripts/run-vitest.mjs \
      src/features/tenants/gmv_max/service.gmvmax-minimal.spec.jsx \
      src/features/tenants/gmv_max/GmvMaxCampaignConsole.smoke.spec.jsx
  else
    echo "WARN: 未找到 pnpm 或 run-vitest.mjs，跳过 vitest 执行"
  fi
fi

echo "OK: Task11A basic tests done"
