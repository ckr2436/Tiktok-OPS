# Operations, Testing, And Deployment

## 1. Runtime

- Repository: `/opt/gmv/GMV-OPS`
- Backend working directory: `/opt/gmv/GMV-OPS/backend`
- Python: `/opt/gmv/python3.13/bin/python`
- Backend environment: `/opt/gmv/GMV-OPS/backend/.env`
- Frontend source: `/opt/gmv/GMV-OPS/gmv-frontend`
- Frontend static deployment: `/opt/gmv/GMV-OPS/frontend`
- FFmpeg: `/opt/apps/bin/ffmpeg`
- Content storage: `/data/gmv_ops/hermes_content_factory`
- Codex: `/opt/apps/codex/codex`

The production worktree is dirty. Never run destructive Git cleanup, hard
reset, or checkout of unrelated files.

## 2. Services

Core:

```text
gmv-api.service
gmv-nginx.service
gmv-celerybeat.service
```

Active workers:

```text
gmv-celery-worker@gmv.tasks.default.service
gmv-celery-worker@gmv.tasks.events.service
gmv-celery-worker@gmv.tasks.hermes_agent.service
gmv-celery-worker@gmv.tasks.ai_video.service
gmv-celery-worker@gmvmax.service
gmv-celery-worker@gmvmax_sync.service
gmv-celery-worker@openai_whisper.service
gmv-celery-worker@website_ads_media.service
```

Inspect effective units with `systemctl cat`; drop-ins define queue
concurrency, browser variables, memory limits, and writable RAID paths.

## 3. Focused Backend Tests

Content Factory and browser:

```bash
cd /opt/gmv/GMV-OPS/backend
PYTHONPATH=. /opt/gmv/python3.13/bin/python -m pytest -q \
  tests/test_content_factory_api_prompts.py \
  tests/test_content_factory_stage_routing.py \
  tests/test_content_factory_variant_cleanup.py \
  tests/test_content_factory_video_dependency_guard.py \
  tests/test_hermes_browser_devices.py \
  tests/test_hermes_browser_recovery.py \
  tests/test_hermes_chatgpt_popups.py \
  tests/test_hermes_segment_only_prompts.py \
  tests/test_hermes_self_heal_state.py \
  tests/test_hermes_slot_wait_recovery_contract.py \
  tests/test_storyboard_split.py
```

AI video:

```bash
cd /opt/gmv/GMV-OPS/backend
PYTHONPATH=. /opt/gmv/python3.13/bin/python -m pytest -q \
  tests/test_ai_video_adapters.py \
  tests/test_ai_video_download_state_machine.py \
  tests/test_ai_video_legacy_cleanup.py \
  tests/test_ai_video_model_routing.py \
  tests/test_bandianwa_video.py
```

Use an isolated database URL/path. Verify `backend/tests/conftest.py` and test
environment before running broad suites. Never point tests at production
MySQL.

## 4. Frontend Tests And Build

Read `gmv-frontend/package.json` for exact scripts. At minimum run the focused
Content Factory and AI-video tests, then the production build.

Build as the `gmv` user so ownership matches deployment:

```bash
cd /opt/gmv/GMV-OPS/gmv-frontend
npm test -- --run
npm run build
```

Deploy the generated static assets to `/opt/gmv/GMV-OPS/frontend` using the
project's established deployment procedure. Verify asset hashes and HTTP 200
responses after Nginx reload/restart.

## 5. Safe Backend Deployment

Before restarting:

1. Check active Hermes stages.
2. Check active Kie/video tasks and live poll heartbeats.
3. Let active work drain when the code change does not require emergency
   interruption.
4. Back up the exact changed source files.
5. Install migrations only after reviewing generated SQL and current revision.

Restart only affected services:

```bash
systemctl restart gmv-api.service
systemctl restart gmv-celery-worker@gmv.tasks.hermes_agent.service
systemctl restart gmv-celery-worker@gmv.tasks.ai_video.service
systemctl restart gmv-celerybeat.service
```

Do not restart every worker by default.

## 6. Verification

After deployment:

```bash
systemctl is-active \
  gmv-api.service \
  gmv-nginx.service \
  gmv-celerybeat.service \
  gmv-celery-worker@gmv.tasks.hermes_agent.service \
  gmv-celery-worker@gmv.tasks.ai_video.service
```

Inspect recent warnings/errors:

```bash
journalctl --since "10 minutes ago" -p warning \
  -u gmv-api.service \
  -u gmv-celery-worker@gmv.tasks.hermes_agent.service \
  -u gmv-celery-worker@gmv.tasks.ai_video.service \
  -u gmv-celerybeat.service \
  --no-pager
```

Also verify:

- health endpoint
- frontend route and current hashed assets
- no duplicate active stage leases
- no duplicate logical video tasks
- no live poller without a heartbeat
- expected local video files exist
- every completed video has a matching guide
- target-count reconciliation is correct
- no cross-user/device slot assignment

## 7. Incident Workflow

When a project is stuck:

1. Identify workspace, user, project, variant, stage, execution token, bridge,
   device, slot, provider task IDs, and local output paths.
2. Determine whether the worker is alive, leased, waiting, rate-limited,
   downloading, composing, paused, or genuinely failed.
3. Inspect the browser conversation before resending.
4. Inspect provider state and local download before replacing a task.
5. Preserve successful variants and repair the smallest failed boundary.
6. Add a regression test for the causal race or validation gap.
7. Deploy the source fix.
8. Requeue/resume the affected project only after the durable fix is live.

A manual SQL update, deleting a failed stage, or calling a task once may recover
the incident but is not the fix.

## 8. Documentation Discipline

After a meaningful architecture change:

- update these handoff files
- update `handoff_manifest.json`
- update focused tests
- record compatibility/migration implications
- never paste secrets or full private user content
