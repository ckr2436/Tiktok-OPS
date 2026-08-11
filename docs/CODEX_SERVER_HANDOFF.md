# GMV OPS Codex Server Handoff

Updated: 2026-07-31

## Purpose

This document transfers the operational context of the long-running GMV OPS
development conversation to Codex running on the GMV server. It intentionally
does not contain passwords, API keys, OAuth secrets, cookies, or hidden prompt
content. Read the repository, live configuration, logs, tests, and official
TikTok API extracts before changing production behavior.

## Primary Repository And Runtime

- Production repository: `/opt/gmv/GMV-OPS`
- Runtime user: `gmv`
- Backend: `/opt/gmv/GMV-OPS/backend`
- Frontend source: `/opt/gmv/GMV-OPS/gmv-frontend`
- Nginx static root: `/opt/gmv/GMV-OPS/frontend`
- API service: `gmv-api.service`
- Web service: `gmv-nginx.service`
- GMV Max workers include `gmv-celery-worker@gmvmax.service` and
  `gmv-celery-worker@gmvmax_sync.service`
- TikTok official API extracts are stored under `apps/api_details` and
  `api_details`. Prefer them over remembered endpoint shapes.
- Production is a dirty working tree. Never revert unrelated changes.

## Product Goal

Build a production-grade, unattended TikTok advertising system:

1. GMV Max campaign creation, editing, pause, resume, delete, product binding,
   identity selection, creative exclusion and scheduled retesting.
2. Product-level intelligent automation with dynamic budget, ROAS bid,
   monitoring frequency, cooldown, daily risk cap and rebuild decisions.
3. Creative-level monitoring that protects spend without killing promising
   creatives after one short sample.
4. Hermes decision and review layers that consume reliable database snapshots,
   generate daily reports, propose parameter changes and retain decision
   evidence.
5. Website Ads for Magento landing pages, including product and media
   libraries, targeting experiments, creative rotation, local media caching and
   report synchronization.
6. A professional Chinese UI with clear states, timestamps, date ranges,
   trustworthy metrics and explicit confirmations for destructive actions.

## Critical Business Rules

- Use the advertiser account timezone for report boundaries. The current main
  advertiser uses `America/New_York`; never derive "today" from server time.
- Decisions must use fresh campaign and creative data. Do not silently mix
  cumulative, daily and ten-minute snapshots.
- Dynamic monitoring can be faster when spend accelerates, but rate limiting
  and request coalescing must prevent API bursts.
- A creative exclusion is normally temporary campaign-level control, followed
  by scheduled retesting across other time windows. One exclusion must not
  automatically become a permanent global blacklist.
- Permanent suppression requires repeated, sufficiently sized evidence across
  tests. Keep the evidence, sample size, time window and reason.
- Creative ID `-1` represents a product card and cannot be excluded through the
  creative API. Product-card overspend must be compared with healthy video
  performance before rebuilding a campaign.
- If video creatives are performing, do not stop a healthy campaign solely
  because the product card is weak.
- Weak individual videos should be excluded or rotated without pausing the
  whole campaign.
- Paused automation remains "intelligent automation" while its strategy is
  active; a temporary campaign pause must not make the product appear
  unautomated.
- Cooldown, test budget, bid, daily cap and rebuild timing are adaptive. Risk
  caps constrain poor performance, but profitable campaigns may scale beyond a
  nominal cap with evidence.
- Long periods of no spend can trigger cancellation/rebuild, but allow enough
  learning time and avoid rapid start/pause loops.
- Website Ads optimize for traffic and `VIEW_CONTENT` when purchase attribution
  is unavailable. Targeting experiments should exclude Alaska and Hawaii when
  required, prefer authentic creator media, and track CTR, CPC, CPM, clicks,
  impressions and video watch metrics.
- Download TikTok CDN video and cover assets to the configured RAID-backed
  local media store because remote URLs expire. Frontends should read local
  cached URLs.

## Hermes Responsibilities

- Execution layer performs validated API operations and records before/after
  state.
- Realtime decision layer evaluates fresh spend, conversion and creative
  evidence.
- Review layer challenges material budget, bid, pause, rebuild and blacklist
  decisions.
- Daily reporting is generated at advertiser-local 00:30 for the previous day.
- Reports and recommendations must distinguish observed facts, derived metrics,
  proposed actions, approved actions and executed actions.
- Do not let an LLM directly mutate ads without deterministic validation,
  idempotency, audit logs and bounded action limits.
- Inspect current Hermes service configuration and model routing rather than
  trusting old conversation model names.

## Latest Completed Change: GMV Max Pause/Delete

The pause/delete chain was audited and fixed on 2026-07-17.

Frontend contract:

```text
POST /api/v1/tenants/{workspace}/providers/{provider}/accounts/{auth}/gmvmax/{campaign_id}/actions
{"type": "pause"}
{"type": "delete"}
```

Backend maps pause/resume/delete to `DISABLE`/`ENABLE`/`DELETE` and calls:

```text
POST /open_api/v1.3/campaign/status/update/
```

Files changed:

- `backend/app/data/db.py`
- `backend/app/features/tenants/ttb/gmv_max/router_provider.py`
- `backend/tests/test_gmvmax_router_action_logs.py`
- `backend/tests/test_db_write_tracking.py`
- `gmv-frontend/src/features/tenants/gmv_max/pages/GmvMaxCampaignDetailPage.jsx`
- `gmv-frontend/src/features/tenants/gmv_max/pages/gmvMaxOverview/ProductAutomationPanel.jsx`

Fixes:

- A reporting sync failure no longer blocks emergency pause/delete.
- Successful actions immediately update local campaign state.
- Delete skips the invalid post-delete `campaign/info` refresh.
- Repeated delete is idempotent when local state is already deleted.
- Raw SQL audit rows and local refresh writes no longer lose their write marker
  through nested transaction startup.
- Frontend errors are visible, and destructive deletion requires confirmation.

Validation:

- 30 focused and adjacent backend tests passed.
- Vite production build passed.
- API and Nginx health checks returned 200.
- No post-restart API error journal entries were found.
- No real campaign was deleted as part of validation.
- Rollback archive:
  `/data/gmv_ops/deploy_backups/pre_gmv_action_fix_20260717_01.tar.gz`

## TikTok Shop Flash-Sale Batch Apply

Product flash-sale editing is a two-phase workflow. Editing one product only
updates the browser-side draft; it does not mutate a policy or enqueue provider
work. After every product is reviewed, the administrator submits one complete
shop plan to:

```text
POST /api/v1/tenants/{workspace}/commerce/flash-sales/apply
```

The request carries the GET response's configuration token. The backend locks
the shop configuration, rejects stale plans, updates every enabled/disabled
product atomically, and publishes exactly one `user_batch_apply` reconciliation
task. A disabled product is removed from replacement activities. If every
product is disabled, the reconciler deactivates the managed activities and
marks all policy revisions applied. A batch task retries lock contention so an
older scheduled reconciliation cannot make the confirmed user plan disappear.

## GMV Max Creative Delivery Status Contract

The live Product GMV Max report was probed on 2026-07-31 against active
campaign `1871600061135218`, and the official report document was regenerated
the same day. The public API still returns the metric
`creative_delivery_status` with these values:

```text
IN_QUEUE
LEARNING
DELIVERING
NOT_DELIVERYING
AUTHORIZATION_NEEDED
EXCLUDED
UNAVAILABLE
REJECTED
NOT_ACTIVE
```

`NOT_DELIVERYING` is TikTok's actual public-API spelling. Preserve these raw
values in storage and API responses. The Ads Manager labels “Exploring”,
“Explored”, “Outstanding”, “Performing”, “Boosting”, and “Boosted” are UI
groupings and are not fields returned by `/gmv_max/report/get/` as of that
probe. The Chinese UI uses the API descriptions as exploration semantics:
`IN_QUEUE` is “待探索”, `LEARNING` is “探索中”, `DELIVERING` is “持续投放”,
and `NOT_DELIVERYING` is “未通过探索”. Unknown future non-empty upstream
values must remain visible as unknown official statuses rather than being
collapsed into local candidate material.

## Deployment Discipline

Before every production edit:

1. Read repository instructions (`AGENTS.md`, project docs and service files).
2. Inspect `git status`; preserve unrelated changes.
3. Compare live file hashes if working from a copied baseline.
4. Back up exact source files and the deployed frontend.
5. Test in an isolated SQLite path; never let tests reset production MySQL.
6. Build the frontend as `gmv`.
7. Deploy frontend assets to `/opt/gmv/GMV-OPS/frontend`.
8. Restart only services that need the changed code.
9. Verify health endpoints, static assets, service state and recent journals.
10. Do not perform destructive TikTok actions merely as a smoke test.

## Resume Checklist

When continuing this work:

1. Treat this handoff as orientation, not as a substitute for current code.
2. Inspect `/opt/gmv/GMV-OPS/.agents`, `.codex`, `AGENTS.md` files and current
   `git status`.
3. Check `gmv-api`, relevant Celery workers, MySQL, Redis and Nginx health.
4. Read recent GMV Max guard, sync and action logs before proposing strategy
   changes.
5. Verify official TikTok API parameters from the local extracted JSON files.
6. State what will be changed before editing, then implement, test, deploy and
   report confirmed outcomes.
