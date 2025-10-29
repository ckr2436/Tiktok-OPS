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

## Migration notes

- **0008_ttb_sync_schedule_stats**: The revision now performs existence checks before
  creating the `schedule_runs.stats_json` column and only drops it during downgrade when
  it is present. If a database already has the column but the Alembic revision is still at
  `0007_platform_policy_v1`, simply run `alembic upgrade head`; the migration will detect
  the pre-existing column and skip the creation without requiring any manual repair steps.

## TikTok Business sync locking

- Sync jobs for the same binding (`workspace_id` + `provider` + `auth_id`) are protected by
  a Redis distributed lock. Keys follow the pattern
  `gmv:locks:{lock_env}:sync:{provider}:{workspace_id}:{auth_id}` by default.
- Configuration knobs:
  - `LOCK_ENV` (defaults to `local`, falls back to `APP_ENV`/`ENV`/`DEPLOY_ENV` when provided)
  - `TTB_SYNC_LOCK_PREFIX` (default `gmv:locks:`)
  - `TTB_SYNC_LOCK_TTL_SECONDS` (default `900`)
  - `TTB_SYNC_LOCK_HEARTBEAT_SECONDS` (default `60`)
  - `TTB_SYNC_USE_DB_LOCKS` (set `true` to fall back to legacy MySQL advisory locks)
- Inspect locks with `redis-cli --scan --pattern 'gmv:locks:*:sync:*'` when debugging, and
  only the lock holder (owner token) is allowed to release the key.
