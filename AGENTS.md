# GMV OPS Repository Instructions

Updated: 2026-08-11 Asia/Shanghai

This is a live production repository with a deliberately dirty worktree. Never
discard, reset, overwrite, or "clean up" changes merely because they are
uncommitted or untracked. Read the current code and database before acting.

## Required Reading

Before changing GMV OPS, read these files in order:

1. `docs/codex-handoff/README.md`
2. `docs/codex-handoff/01_SYSTEM_BOUNDARIES.md`
3. `docs/codex-handoff/02_CONTENT_FACTORY.md`
4. `docs/codex-handoff/03_PROVIDERS_AND_BROWSER_BRIDGE.md`
5. `docs/codex-handoff/04_OPERATIONS_AND_TESTING.md`
6. `docs/codex-handoff/05_CURRENT_STATE_AND_REGRESSIONS.md`
7. `docs/CODEX_SERVER_HANDOFF.md` for the separate GMV Max history

The machine-readable inventory is
`docs/codex-handoff/handoff_manifest.json`.

## Non-Negotiable Rules

- Preserve workspace, user, device, project, variant, and task isolation at
  every API, query, filesystem, queue, browser, and download boundary.
- A project owns at most one browser slot at a time. Never move a running
  project across slots or read another slot's ChatGPT conversation.
- API-first execution is preferred. Start a browser slot only when a stage
  actually needs browser fallback.
- Every stage submission must be idempotent. A timeout or late response must
  not create a second ChatGPT message, conversation, video task, or variant.
- Uploaded character anchors guide visual generation only. Video providers
  receive generated final references, plus the authoritative uploaded product
  image when required. Do not send the original character anchor to video
  generation.
- The uploaded product package is authoritative. AI may place that product in
  a scene, but must not invent or regenerate a white-background product anchor.
- Creative owns the complete story, full script, voice identity, conversion
  arc, and per-segment allocation. Video prompt compilation receives that
  structure but emits only the current segment's timeline, action, camera,
  dialogue, continuity, and negative constraints.
- Reference images and prompts have separate jobs. References lock character,
  scene, product, and key action. Prompt text must not repeat a whole-project
  packet or unrelated stages.
- A requested target count means completed full videos, not attempted variants
  or loose clips. Failed variants are cleaned and replenished until the target
  is met or the project reaches an explicit bounded quality pause.
- A provider task is not successful until the result is downloaded to local
  RAID storage. Downloads, polling, retries, composition, and guide generation
  remain asynchronous.
- One edit/publish guide belongs to one completed video. It must correspond to
  that video's real beats and contain only a useful title, no more than five
  hashtags, and chapter overlay guidance.
- Manual user pause and automatic quality pause are different states. An
  automatic quality pause must drain already-submitted videos and guides but
  must not start another creative variant. A later explicit user pause wins.
- Do not restore removed private-GPT routing, Sora 2/KIE legacy pages, video QA
  approval gates, or project-wide single edit guides.
- Never expose API keys, passwords, cookies, OAuth secrets, private prompts, or
  session data in source, tests, documentation, logs, or chat output.

## Engineering Workflow

1. Inspect `git status --short` and preserve unrelated work.
2. Read the exact source, tests, migrations, service units, and live logs for
   the affected path. Do not rely on this handoff alone.
3. Reproduce the root cause. Avoid one-row database edits as the solution.
4. Make a durable source-level change with bounded retries, leases,
   idempotency, auditability, and explicit state transitions.
5. Add focused regression tests for the reported failure and adjacent race
   conditions.
6. Run tests against an isolated database path. Never reset production MySQL.
7. Check active Celery work before restarting workers.
8. Deploy only the changed backend/frontend/agent artifacts.
9. Verify service state, health endpoints, local output files, queue state, and
   recent journals.
10. Report only confirmed results and any remaining risk.

## Primary Paths

- Repository: `/opt/gmv/GMV-OPS`
- Backend: `/opt/gmv/GMV-OPS/backend`
- Frontend source: `/opt/gmv/GMV-OPS/gmv-frontend`
- Deployed frontend: `/opt/gmv/GMV-OPS/frontend`
- Bridge agent source: `/opt/gmv/GMV-OPS/hermes-bridge-agent`
- Bridge executable: `/opt/gmv/GMV-OPS/backend/assets/MYUPONA-HermesBridge.exe`
- Content storage: `/data/gmv_ops/hermes_content_factory`
- Python: `/opt/gmv/python3.13/bin/python`
- FFmpeg: `/opt/apps/bin/ffmpeg`
- Codex: `/opt/apps/codex/codex` and `/usr/local/bin/codex`

## Key Services

- `gmv-api.service`
- `gmv-nginx.service`
- `gmv-celerybeat.service`
- `gmv-celery-worker@gmv.tasks.hermes_agent.service`
- `gmv-celery-worker@gmv.tasks.ai_video.api.service`
- `gmv-celery-worker@gmv.tasks.ai_video.browser.service`
- `gmv-celery-worker@gmv.tasks.ai_video.browser_poll.service`
- `gmv-celery-worker@gmv.tasks.ai_video.download.service`
- `gmv-celery-worker@gmv.tasks.ai_video.maintenance.service`
- `gmv-celery-worker@gmv.tasks.hermes_maintenance.service`
- `gmv-celery-worker@openai_whisper.service`
- `gmv-celery-worker@gmvmax.service`
- `gmv-celery-worker@gmvmax_sync.service`
- `gmv-celery-worker@website_ads_media.service`

## High-Risk Source Files

- `backend/app/tasks/hermes_agent/content_factory_tasks.py`
- `backend/app/services/hermes_agent/direct_browser.py`
- `backend/app/services/hermes_agent/content_factory.py`
- `backend/app/services/hermes_agent/content_factory_api.py`
- `backend/app/features/tenants/hermes_agent/router.py`
- `backend/app/tasks/ai_video/video_tasks.py`
- `backend/app/services/ai_video/accounts.py`
- `backend/app/data/models/hermes_agent.py`
- `backend/app/data/models/kie_api.py`
- `gmv-frontend/src/features/tenants/hermes_agent/pages/ContentFactoryPage.jsx`

These files contain intertwined state-machine, idempotency, tenancy, browser,
provider, and recovery behavior. Make narrow changes and run the focused suites.
