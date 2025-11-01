# GMV Backend v2

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn gunicorn sqlalchemy pymysql passlib[bcrypt] itsdangerous pydantic pydantic-settings
export DATABASE_URL='mysql+pymysql://gmv:gmv@127.0.0.1:3306/gmv?charset=utf8mb4'
uvicorn app.app:app --host 0.0.0.0 --port 8000
```

## Env
- Put `.env` beside `app/` or in project root:
```
APP_VERSION=2.0.0
CORS_ORIGINS=https://gmv.drafyn.com,http://localhost:5173
DATABASE_URL=mysql+pymysql://gmv:gmv@127.0.0.1:3306/gmv?charset=utf8mb4
SECRET_KEY=change_me
COOKIE_NAME=gmv_session
COOKIE_MAX_AGE=604800
COOKIE_SECURE=True
COOKIE_SAMESITE=lax
```

## Breaking changes

- 2025-11-01: Renamed all TikTok Business shop entities and APIs to store/store_id. Existing migrations upgrade database schema and task catalog entries automatically; downstream integrations must update to the new naming.
- 2025-11-12: Replaced legacy TikTok Business data browsing endpoints with a GMV Max management workflow. Only metadata dropdown sources, the GMV Max binding configuration, and product sync remain available; legacy list/chart APIs now return `TTB_LEGACY_DISABLED` (HTTP 410).
- 2025-11-12: Added the `ttb.sync.meta` periodic task recommendation and GMV Max auto-sync scheduler wiring; refresh jobs now run through the new binding configuration service.
- 2025-11-15: GMV Max advertiser hydration now calls `/advertiser/info/` in batches after each sync to populate currency, timezone、display_timezone、country、industry、status 以及 owner_bc_id；选项接口 `/gmv-max/options` 提供链路缓存与手动刷新，前端基于返回的 `links` 在本地联动过滤下拉列表，并使用 `refresh=timeout` 反馈提示。

## Tenant TikTok Business API quick reference

```bash
# list bindings inside workspace 42
curl -H 'Authorization: Bearer <token>' \
  https://gmv.local/api/v1/tenants/42/providers

# list TikTok Business accounts for workspace 42
curl -H 'Authorization: Bearer <token>' \
  'https://gmv.local/api/v1/tenants/42/providers/tiktok-business/accounts?page=1&page_size=20'

# fetch cached GMV Max options (ETag aware)
curl -H 'Authorization: Bearer <token>' \
  https://gmv.local/api/v1/tenants/42/providers/tiktok-business/accounts/7/gmv-max/options

# trigger background refresh (returns immediately with {"refresh":"timeout","idempotency_key":...} when no change within 3s)
curl -H 'Authorization: Bearer <token>' \
  'https://gmv.local/api/v1/tenants/42/providers/tiktok-business/accounts/7/gmv-max/options?refresh=1'

# trigger GMV Max product sync for an advertiser/store pair
curl -X POST -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  https://gmv.local/api/v1/tenants/42/providers/tiktok-business/accounts/7/sync \
  -d '{"scope":"products","mode":"full","options":{"advertiser_id":"ADV123","store_id":"STORE456","eligibility":"gmv_max"}}'

# fetch GMV Max binding configuration
curl -H 'Authorization: Bearer <token>' \
  https://gmv.local/api/v1/tenants/42/providers/tiktok-business/accounts/7/gmv-max/config

# inspect run 123 for auth_id=7
curl -H 'Authorization: Bearer <token>' \
  https://gmv.local/api/v1/tenants/42/providers/tiktok-business/accounts/7/sync-runs/123
```
