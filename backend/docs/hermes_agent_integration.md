# Hermes Agent integration for GMV Ops

This branch integrates Hermes Agent as a tenant-scoped AI growth service for GMV Ops.

## Architecture

Hermes Agent must run as a local-only internal service. The browser must never call Hermes directly.

```text
Browser
  -> GMV Ops FastAPI session / tenant auth
  -> /api/v1/tenants/{workspace_id}/hermes-agent/*
  -> http://127.0.0.1:8642/v1/responses
```

GMV Ops owns authentication, workspace isolation, feature permissions, audit logs, run history, and task status. Hermes Agent owns agent execution.

## Required Hermes runtime

Run Hermes separately on the same Linux host:

```bash
hermes gateway
```

Recommended Hermes `~/.hermes/.env`:

```env
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=<long-random-secret>
API_SERVER_MODEL_NAME=gmv-ops-hermes
```

Do not bind Hermes to `0.0.0.0` unless it is behind strict internal networking.

## GMV Ops environment variables

Add to the backend `.env`:

```env
HERMES_AGENT_ENABLED=true
HERMES_AGENT_BASE_URL=http://127.0.0.1:8642/v1
HERMES_AGENT_API_KEY=<same as Hermes API_SERVER_KEY>
HERMES_AGENT_MODEL=gmv-ops-hermes
HERMES_AGENT_TIMEOUT_SECONDS=120
HERMES_AGENT_TASK_QUEUE=gmv.tasks.hermes_agent
HERMES_AGENT_ALLOW_MEMBER=true
HERMES_AGENT_REQUIRE_EXPLICIT_PERMISSION=false
HERMES_AGENT_MAX_INPUT_CHARS=30000
HERMES_AGENT_MAX_RESULT_CHARS=200000
```

Production permission modes:

- `HERMES_AGENT_ALLOW_MEMBER=true`, `HERMES_AGENT_REQUIRE_EXPLICIT_PERMISSION=false`: all tenant members can use Hermes.
- `HERMES_AGENT_ALLOW_MEMBER=true`, `HERMES_AGENT_REQUIRE_EXPLICIT_PERMISSION=true`: tenant members must have feature permissions.
- `HERMES_AGENT_ALLOW_MEMBER=false`: only tenant owner/admin can use Hermes.

## Database migration

Run:

```bash
cd backend
alembic upgrade head
```

New tables:

- `user_feature_permissions`
- `hermes_agent_conversations`
- `hermes_agent_messages`
- `hermes_agent_runs`

## Celery

The queue `gmv.tasks.hermes_agent` is registered automatically. Make sure at least one worker consumes it:

```bash
celery -A app.celery_app.celery_app worker -Q gmv.tasks.hermes_agent,gmv.tasks.default --loglevel=INFO
```

Async Hermes requests use task name:

```text
hermes_agent.run
```

## API surface

All APIs require the existing GMV Ops cookie session and tenant membership.

Base path:

```text
/api/v1/tenants/{workspace_id}/hermes-agent
```

### Capabilities

```http
GET /capabilities
```

Returns enabled flag, task types, model name, and permission mode.

### Health

```http
GET /health
```

Proxies to the local Hermes health endpoint.

### Generic run

```http
POST /runs
```

Example:

```json
{
  "task_type": "script",
  "title": "Sleep Ease Gummies 15s TikTok script",
  "input": "Write a 15 second UGC script about brain won't shut off before bed.",
  "input_json": {
    "product": "Sleep Ease Gummies",
    "duration": 15,
    "style": "UGC"
  },
  "workspace_context": {
    "brand": "MYUPONA"
  },
  "conversation_key": "sleep-script",
  "async_mode": false
}
```

Supported `task_type` values:

- `general`
- `seo`
- `geo`
- `video_analysis`
- `script`
- `product_copy`

### Specialized shortcuts

```http
POST /seo
POST /geo
POST /video-analysis
POST /script
POST /product-copy
```

Body shape:

```json
{
  "title": "Optional title",
  "input": "User task text",
  "input_json": {},
  "workspace_context": {"brand": "MYUPONA"},
  "conversation_key": "optional-conversation-key",
  "async_mode": false
}
```

### Run history

```http
GET /runs?mine=true&task_type=script&status=success&limit=20&offset=0
GET /runs/{run_id}
```

### Conversations

```http
GET /conversations?mine=true
GET /conversations/{conversation_id}/messages
```

### Feature permissions

Tenant owner/admin only:

```http
GET /permissions
PUT /permissions
```

Example:

```json
{
  "user_id": 123,
  "feature_key": "hermes_agent.script",
  "is_enabled": true
}
```

Available feature keys:

- `hermes_agent.use`
- `hermes_agent.seo`
- `hermes_agent.geo`
- `hermes_agent.video_analysis`
- `hermes_agent.script`
- `hermes_agent.product_copy`

## Frontend integration contract

The repository currently does not contain a detectable React/Vite/Next frontend package. Frontend should call the FastAPI tenant endpoints only, with cookies enabled.

Example browser call:

```ts
await fetch(`/api/v1/tenants/${workspaceId}/hermes-agent/script`, {
  method: 'POST',
  credentials: 'include',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    title: 'Sleep Ease Gummies script',
    input: 'Generate a 15s TikTok UGC script about brain won\'t shut off.',
    input_json: {product: 'Sleep Ease Gummies', duration: 15},
    workspace_context: {brand: 'MYUPONA'},
    async_mode: false
  })
})
```

Never call `http://127.0.0.1:8642` from the browser.

## Operational notes

- Sync calls are simple and convenient for short prompts.
- Use `async_mode=true` for long SEO/GEO reports or video breakdowns.
- All runs are stored in `hermes_agent_runs`.
- User and assistant messages are stored in `hermes_agent_messages`.
- Audit events use actions such as `hermes_agent.run.create`, `hermes_agent.run.success`, `hermes_agent.run.failed`, and `hermes_agent.permission.set`.
