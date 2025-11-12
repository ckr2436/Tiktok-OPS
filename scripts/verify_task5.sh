#!/usr/bin/env bash
set -euo pipefail

echo "=== Task5 自检开始 ==="

# 0) 环境信息
echo "[0] 环境信息"
python -V || true
pytest --version || true
echo "当前分支：$(git rev-parse --abbrev-ref HEAD)"
echo

# 1) 工厂是否存在
echo "[1] 检查工厂文件是否存在"
if [ -f backend/app/services/ttb_client_factory.py ]; then
  echo "OK: backend/app/services/ttb_client_factory.py 存在"
else
  echo "FAIL: 缺少 backend/app/services/ttb_client_factory.py"
  exit 1
fi
echo

# 2) 禁止直接 new TTBApiClient（除了工厂）
echo "[2] 扫描直接构造 TTBApiClient 的位置（应为空）"
FOUND_TTB=$(git grep -n "TTBApiClient(" -- backend | grep -v "ttb_client_factory.py" || true)
if [ -n "${FOUND_TTB:-}" ]; then
  echo "FAIL: 仍有直接 new TTBApiClient 的地方："
  echo "$FOUND_TTB"
  exit 1
else
  echo "OK: 未发现除工厂以外的 TTBApiClient 构造"
fi
echo

# 3) 检查 build_ttb_client 的使用情况
echo "[3] 统计 build_ttb_client 的使用点"
git grep -n "build_ttb_client" -- backend || echo "WARN: 未找到 build_ttb_client 引用，请确认是否被路由/脚本使用"
echo

# 4) 扫描直连 http 调用（应只出现在统一 http 层）
echo "[4] 扫描 httpx/requests 直连调用"
git grep -n -E "httpx\\.|requests\\." -- backend | grep -v "services/ttb_http" || true
echo

# 5) GMV Max 路由必须 async
echo "[5] 检查 GMV Max 路由是否 async"
GMV_DIR="backend/app/features/tenants/ttb/gmv_max"
if [ -d "$GMV_DIR" ]; then
  for f in "$GMV_DIR"/*.py; do
    echo "--- 检查 $f"
    if ! grep -q "async def " "$f"; then
      echo "FAIL: $f 中未发现 async 路由定义"
      exit 1
    fi
  done
  echo "OK: GMV Max 路由文件中均存在 async 定义"
else
  echo "WARN: 目录 $GMV_DIR 不存在，请确认 GMV Max 路由位置"
fi
echo

# 6) Alembic 迁移中是否新增唯一约束（简要正则检查）
echo "[6] 检查 Alembic 迁移是否包含唯一约束关键字（用于 GMV Max 表）"
git grep -n -E "UniqueConstraint|unique=True|UNIQUE" -- backend/migrations/versions || echo "WARN: 未检出唯一约束关键字，请人工查看迁移文件"
echo

# 7) 运行最小单测集
echo "[7] 运行关键单测文件"
pytest -q backend/tests/test_ttb_client_factory.py
pytest -q backend/tests/test_gmvmax_actions.py
pytest -q backend/tests/test_gmvmax_metrics_idempotent.py
echo "OK: 关键单测通过"
echo

echo "=== Task5 自检通过: 工厂统一 / 路由 async / 唯一约束提示 / 单测通过 ==="
