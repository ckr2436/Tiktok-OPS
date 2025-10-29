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

## Tenant TikTok Business API quick reference

```bash
# list bindings inside workspace 42
curl -H 'Authorization: Bearer <token>' \
  https://gmv.local/api/v1/tenants/42/providers

# list TikTok Business accounts for workspace 42
curl -H 'Authorization: Bearer <token>' \
  'https://gmv.local/api/v1/tenants/42/providers/tiktok-business/accounts?page=1&page_size=20'

# trigger incremental sync for auth_id=7
curl -X POST -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  https://gmv.local/api/v1/tenants/42/providers/tiktok-business/accounts/7/sync \
  -d '{"scope":"all","mode":"incremental","idempotency_key":"demo-42"}'

# inspect run 123 for auth_id=7
curl -H 'Authorization: Bearer <token>' \
  https://gmv.local/api/v1/tenants/42/providers/tiktok-business/accounts/7/sync-runs/123
```
