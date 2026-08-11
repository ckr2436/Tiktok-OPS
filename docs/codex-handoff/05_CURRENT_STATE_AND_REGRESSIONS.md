# Current State And Anti-Regression History

Updated: 2026-07-28 Asia/Shanghai

## 0. Creative Intent Execution Architecture

Member project creation now has one authority: the AI Producer conversation.
The former direct create route, manual advanced form, frontend create helper,
and create-request schema were removed. Producer confirmation requires a
signed version-2 `CreativeIntentManifest`; it cannot create a project from an
unstructured summary alone.

The semantic execution chain is:

```text
user or attachment evidence
  -> signed R-NNN requirements
  -> Director script/capability/segment mappings
  -> Production Plan beat/reference/audio/copy mappings
  -> segment-local requirement contracts
  -> provider render and segment evidence review
  -> composed-video final intent guardian
```

Critical and high requirements must survive every mapping boundary. Generic
booleans and action-count heuristics are not acceptable substitutes for a
request such as preserving a benchmark hook mechanism, emotional escalation,
or conversion bridge. The final guardian evaluates originality separately
from effectiveness transfer and can authorize bounded segment regeneration
without rewriting already accepted copy or restarting an unrelated segment.

## 1. Verified Production State At Handoff

The final health verification immediately before this handoff found:

- `gmv-api.service`: active
- `gmv-celery-worker@gmv.tasks.hermes_agent.service`: active
- `gmv-celery-worker@gmv.tasks.hermes_maintenance.service`: active
- `gmv-celery-worker@gmv.tasks.ai_video.api.service`: active, concurrency 3
- `gmv-celery-worker@gmv.tasks.ai_video.browser.service`: active, concurrency 3
- `gmv-celery-worker@gmv.tasks.ai_video.browser_poll.service`: active, concurrency 2
- `gmv-celery-worker@gmv.tasks.ai_video.download.service`: active, solo
- `gmv-celery-worker@gmv.tasks.ai_video.maintenance.service`: active, solo
- `gmv-celerybeat.service`: active
- active Content Factory stages: 0
- active AI video rows: 0
- live video pollers: 0
- warning-level entries in the checked recent service journal window: none

The retired monolithic `gmv.tasks.ai_video` queue and worker are absent. API
providers, browser-backed Doubao production, result downloads, account/lab
maintenance, and Hermes browser maintenance have independent queues. Provider
failover republishes to the replacement provider's production lane instead of
performing cross-lane I/O inside the old delivery.

The focused Content Factory test file passed:

```text
110 passed
```

An older broad backend run had unrelated legacy failures in Whisper, WebShell,
and keyboard mocks. Do not claim the entire repository suite is green without
rerunning it.

## 2. Project 166 Recovery

Project:

- database ID: `166`
- project key: `cf_e64eaf7b1b894012b893`
- workspace: `3`
- user: `6`
- target complete videos: `2`

It had submitted variant 1 before an automatic quality pause stopped variant 2.
The repaired behavior drained submitted work without opening a new variant.

Verified outputs:

- provider/local task IDs `2400` through `2408`: success
- complete video: `模仿1-1-v01-1.mp4`
- per-video guide: `模仿1-1-v01-1-editor-guide.md`
- both files existed locally
- project remained paused at the quality boundary
- completed indices: `[1]`
- submitted indices: `[1]`

The pause message explicitly states that one of two videos completed and no new
creative variant was started.

## 3. Latest Durable Fixes

### Pause ownership

Files:

- `backend/app/services/hermes_agent/content_factory.py`
- `backend/app/tasks/hermes_agent/content_factory_tasks.py`

Manual user pause and automatic quality pause now use distinct ownership
metadata. Resume/restart clears obsolete pause metadata. A newer quality pause
can drain submitted work despite older stale manual metadata, while a later
explicit user pause remains authoritative.

### Submitted-video drain

The waiter continues provider polling, download, composition, and edit-guide
generation during automatic quality pause. It keeps the project paused and
does not queue another creative variant.

### Poll owner race

File:

`backend/app/tasks/ai_video/video_tasks.py`

The video worker now claims one poll owner before provider network work,
maintains a heartbeat, and prevents stale orphan recovery from finalizing a
live task. Automatic-quality-paused submitted tasks are not considered
abandoned.

### Segment-only prompt compiler

Files:

- `backend/app/services/hermes_agent/content_factory_api.py`
- `backend/app/tasks/hermes_agent/content_factory_tasks.py`

The structured compiler treats `prompt` as canonical, accepts legacy
`short_prompt` only at the boundary, rejects contaminated payloads, and emits
segment-local prompts with authoritative reference ordering.

### Semantic copy budget and API-only recovery

Files:

- `backend/app/services/hermes_agent/content_factory_api.py`
- `backend/app/tasks/hermes_agent/content_factory_tasks.py`

The API and browser creative routes now share one concrete per-segment spoken
copy budget. Product videos must begin the solution/product bridge before the
final segment so the last ten seconds do not carry every ingredient, offer,
and CTA at once. A complete API response that still violates deterministic
copy, cast, timeline, or review contracts is regenerated with the exact local
validation error. After the bounded response budget, CREATIVE receives a
bounded fresh API-only concept replan with a rotated diversity lane. Other
semantic exhaustion, or exhausted creative replans, creates an automatic
quality pause. Semantic content errors never wake a browser slot.

Per-video guide end cards reduce an authorized dollar promotion to the exact
price token so the final three-second overlay stays readable.

### Durable deliverable authority and parallel resume

Files:

- `backend/app/tasks/hermes_agent/content_factory_tasks.py`
- `backend/app/services/hermes_agent/content_factory.py`
- `backend/app/features/tenants/hermes_agent/router.py`

`video_variant_pipeline.completed_indices` is scheduling metadata, not
completion evidence. Completion is now rebuilt from project-owned video asset
rows whose local RAID files still exist. A quality reset or variant cleanup
therefore cannot leave stale indices that make a target-count project finish
with too few MP4 files. Concurrent VIDEO_PROMPTS and waiter commits also
reconcile against the same durable assets, so a stale stage snapshot cannot
temporarily remove or resurrect a completed deliverable.

Manual resume now restores the one project-global video waiter whenever the
project still owns AI video task IDs, even if the bounded parallel pipeline has
already moved `current_stage` back to a creative or visual stage. This prevents
dependency-chained segments from remaining idle after a safe pause/deploy.
API-only stages keep the browser slot dormant throughout this recovery.

Visual-provider retry persistence re-stamps the current self-heal policy on
both progress commits and outer exception commits. A stale checkpoint can no
longer downgrade a successor retry row to an older policy version.

### Atomic visual repair, creative replay isolation, and guide authority

Files:

- `backend/app/services/hermes_agent/content_factory_api.py`
- `backend/app/tasks/hermes_agent/content_factory_tasks.py`
- `backend/app/services/hermes_agent/content_factory.py`

Pixel-grounded creative review now canonicalizes every blocking per-reference
verdict before it derives the top-level approval. A required product placement
surface reported as `not_required`, for example, becomes a row-local mismatch
instead of producing a rejection with no failed reference index. Targeted
repair therefore keeps approved files and regenerates only failed native
references.

When no uploaded character anchor exists, targeted repair attaches the
approved first reference as identity, wardrobe, illustration-medium, and room
authority. The current segment prompt still owns pose, composition, and
action, preventing both text-only identity drift and opening-frame cloning.

CREATIVE durable-response fingerprints include the assigned diversity lane
and prior-variant briefs. A quality-resume lane change cannot replay the
rejected concept. Cross-variant duplicate responses, including old complete
captures, go through a bounded fresh text-API regeneration instead of a
15-minute capture-validation cooldown. Every delivery and retry persistence
boundary re-stamps the loaded self-heal policy.

Editor-guide readiness is rebuilt from project-owned guide assets whose local
RAID files exist and whose video index has a completed local MP4. Missing
guides are regenerated deterministically from the completed video; stale
historical guide IDs can no longer inflate progress or final completion.

## 4. Anti-Regression Incident List

Every item below has occurred before. Test these boundaries whenever adjacent
code changes.

### Provenance and splitting

- ChatGPT generated a board, but the system captured the uploaded product image
  as `VISUAL_PREVIEW`.
- Final assets were split from product/supplement-facts images instead of the
  generated board.
- Equal-grid cropping ignored divider lines and cut panels incorrectly.
- Creative requested N references, visual output had M, and review incorrectly
  passed it.
- A retry kept reusing an unrelated image.

Required prevention: strict assistant-generated provenance, stage/variant
asset selection, divider-aware splitting, expected-count validation, and
bounded visual repair.

### Duplicate execution

- One stage opened two ChatGPT conversations.
- A completed response was not recognized and the same prompt was sent again.
- One logical video segment created several visible tasks.
- Recovery and the live poll worker raced on the same upstream task.

Required prevention: execution tokens, leases, conversation URL ownership,
late-response scan before resend, logical task idempotency, poll claim owner,
and heartbeat-aware recovery.

### Cross-user/device contamination

- Other users' projects reached the original operator's browser.
- Linux file paths were passed to Windows Chrome.
- Clipboard transfer mixed files.
- One project moved between slots or a newly created slot lacked the logged-in
  ChatGPT account.

Required prevention: workspace/user/device binding, selected-device state,
local inbox sync confirmation, one project/one slot, no clipboard, and
device-local slot pool UI.

### Rate limits and popups

- Chinese "requests too frequent" modal remained for a long time.
- "Got it" was not clicked.
- Upgrade/promotional banners blocked capture.
- Fixed/global wait state caused unnecessary throttling across projects.

Required prevention: bilingual semantic popup handling, safe close actions,
project-local adaptive cooldown, human-like pacing, and completed-response
inspection before retry.

### Prompt/reference contamination

- Whole-project JSON was embedded in a segment prompt.
- CTA text included Python/JSON fragments.
- Every final reference was sent to every segment.
- Uploaded character anchors were sent directly to video generation.
- Product package changed or gummies came out of the bottle bottom.
- Two segments each tried to tell the complete story.

Required prevention: Creative owns full script, deterministic segment compiler,
semantic reference selection, generated character/scene references for video,
authoritative uploaded product anchor, and first-frame continuation.

### Doubao account-pool isolation

- Adding a second account reused the first account's browser Profile.
- Background health work opened Chrome even when no login was requested.
- A text-only provider could be selected for a task carrying reference media.
- Concurrent tasks could submit through the same account.
- The edited dynamic-watermark delivery could be mistaken for the original
  generated resource.

Required prevention: immutable Profile per account, fixed proxy, encrypted
session context, one generation lease per account, HTTP-only scheduled strong
authentication probe, separate network/capability/capacity states, exact
capability filtering, fail-closed original-resource lookup, and unified
provider failover after account-level cooldown/disable. Never restore the old
"HTTP 200 equals healthy session" keepalive rule: Doubao returns 200 for logged
out visitors too. Serialize only the browser submission phase per shared proxy
exit, release that lane after upstream acceptance, and reject expired browser
markers so stale work cannot create an open/close loop.
Do not restore pure-LRU account choice: route ready accounts by recent real
success, fresh probes, consecutive-error penalty and submit latency, while
keeping LRU only as a tie-breaker. Preserve bounded, secret-free per-account
submit phase telemetry so account startup latency is not confused with Doubao
remote generation latency.

### Flow grant lifecycle

- The account page treated `is_active`, web login, stored AT, and routability as
  the same state.
- The one-hour keepalive interval raced the short-lived Flow access grant.
- A successful Windows browser capture could return an account to the pool
  without enabling its lifecycle keepalive row.
- Expired accounts remained visually counted as enabled and their credits were
  included even though routing correctly failed closed.

Required prevention: publish persistent HTTP keepalive atomically with verified
browser capture, refresh every 20 minutes, route only unbanned authorization-
valid accounts, and show administrator state separately from authorization and
routability. After HTTP recovery fails with `GRANT_EXPIRED`, Hermes may perform
one headless capture from the immutable fixed Profile per continuous grant
episode. Failed capture or login/account verification becomes human-required;
it must never trigger a visible or repeated unattended browser loop.

### Target count and deliverables

- Project completed with 9 videos when target was 10.
- Failed variants were not replenished.
- Retrying one variant removed earlier successful records.
- One project-wide edit guide replaced per-video guides.
- Guides contained generic placeholders or internal reference text.
- Provider reported success but local download remained stuck.

Required prevention: completed-count reconciliation, replacement variants,
variant-scoped cleanup, per-video guide identity, guide schema validation,
async download retries, remote fallback URL, and local-file completion gate.

### Slot lifecycle

- A single project opened many slots.
- Browser repeatedly opened and closed in a loop.
- CDP was manually closed and the project never resumed.
- Reconnect created a new bridge although the old bridge was already logged in.
- A stopped project retained a slot and blocked another project.

Required prevention: API-first routing, demand-based slot creation,
project-slot pinning, explicit API-video `dormant` state, heartbeat grace
periods, bounded reconnect, and release on pause/delete/complete/failure. A
controlled dormant acknowledgement must never be treated as a CDP outage or
reassigned to a different Chrome profile.

The Agent desired-slot response must resolve dormancy with the latest active
stage and its `execution_backend`, not only `project.current_stage`. A bounded
parallel project can be in `CREATIVE` or `CREATIVE_REVIEW` while prior video
tasks are still running; if that stage is API-backed, the sticky profile stays
reserved but Chrome and its SSH tunnel remain dormant. When a genuine browser
fallback returns to API, re-hibernate the same sticky slot atomically instead
of releasing it and allowing project-state lease repair to wake it again.

### Full-history creative duplicate gate

The provider prompt intentionally receives at most eight compact prior-variant
briefs, but this bounded prompt view must never become the server's approval
view. Before prompt compaction, the stage runner now preserves the complete
durable creative history (up to the project history bound) and uses that
authoritative list for the output-side semantic duplicate gate. This prevents
an older shipped concept from being repeated after enough newer variants push
it outside the model prompt window.

The duplicate gate also canonicalizes conservative story motifs. A concept is
rejected when it repeats the same concrete activity domain plus at least two
relationship/loss identity motifs, even when the model swaps surface synonyms
such as motorcycle/bicycle, friend/partner, or reliability/trust. Core loss and
turning point are part of the durable fingerprint; generic night-routine
actions remain excluded.

Every cross-variant duplicate rejection also advances the current variant's
durable diversity-lane offset before the fresh API regeneration. The retry
therefore receives a genuinely different story scaffold instead of being told
to change while remaining pinned to the same lane.

Provider context still receives only the latest compact brief per variant, but
the server validation registry retains every successful creative attempt. A
sparse final replan can no longer erase the richer occupation, relationship,
loss, and turning-point fingerprint from an earlier attempt. The authoritative
registry is stripped before provider submission and is used only by the local
fail-closed output gate.

A complete CREATIVE payload that omits a complete opening, development, and
resolution is classified as a semantic generation failure even when recovered
from a durable response capture. It must receive a fresh API generation rather
than replaying the same structurally invalid payload and failing the project.

### Variant-scoped restart guide isolation

Legacy composed videos and per-video edit guidance store their authoritative
variant identity as `content_factory_video_index`. Variant-scoped restart
cleanup must read that field before any fallback to the currently active
variant. Otherwise every older video or guide without a separate
`content_factory_variant_index` is misclassified as the active variant and
deleted.

Restart cleanup now removes only the active variant's downstream assets and
atomically prunes deleted video/guide asset IDs plus deleted guide indices from
project state. Completed variants retain both their local Markdown guide and
database asset row. Reconciliation must derive completion from existing local
files and rows, never from cached guide IDs alone.

The server-side browser inbox is a second delivery copy written only after a
complete local video has been composed. Reconciliation now uses that
project-scoped copy as bounded recovery evidence when a legacy cleanup removed
the primary file or database row: filename index, target range, minimum size,
and FFprobe duration must all pass. Recovery atomically restores the primary
MP4, rebuilds its metadata from the durable `VIDEO_PROMPTS` stage, updates
completion state, and recreates its per-video guide without submitting another
provider task.

### Manual resume must distinguish history from pending video work

`ai_video_task_ids` is durable submission history, not a list of work that is
still running. Manual resume must resolve those IDs against current provider
task states and retain only genuinely non-terminal IDs in
`ai_video_pending_task_ids`. A historical success, failure, timeout, or error
must never restart the global video waiter or prevent the currently paused
creative stage from being queued.

The resume transaction now clears stale waiter identity and heartbeat fields
when no pending task remains. Missing provider rows are treated as pending only
when the project had already declared those IDs pending, which preserves
fail-closed behavior without turning all historical IDs into phantom work.

### Spoken-copy hard limits are one shared contract

The creative prompt and deterministic creative validator consume one shared
per-segment spoken-copy budget. For a 40-second US-English video split into
four 10-second segments, every segment has a hard maximum of 18 spoken words,
including narration, quoted dialogue, product features, price, and CTA.

A model acknowledgement such as "this segment exceeds the hard limit" is
evidence of a contract failure, never permission to pass it. The secondary
voice-rate plausibility check may reject even shorter copy, but its fast-
delivery tolerance can no longer admit copy above the prompt's hard maximum.
Creative is regenerated through the bounded API semantic-repair path before
any visual or video provider receives the invalid script.

### Reviewer schema errors must never redraw valid images

`CREATIVE_REVIEW.reference_checks[].observed_facts` and
`missing_or_wrong_facts` are evidence arrays. If the text reviewer returns a
prose string or another non-list value, that is a reviewer output-contract
failure, not pixel evidence that the source image is wrong.

The server now raises those container-type failures into the bounded text
semantic re-review path. It does not synthesize a character mismatch, create a
partial visual-repair request, or spend image quota. Only a structurally valid
row with concrete pixel-grounded missing/wrong facts may cause an image to be
regenerated.

### Multimodal semantic authority replaces fixed creative gates

The current Director, independent Critic, and visual-inspector logical roles
all require `multimodal` routes. This remains true for a stage whose current
packet contains only text: the role is still assigned to a model class that can
inspect images and video evidence when the workflow supplies them.

Creative meaning is model-owned. Story quality, hook transfer, visual
continuity, product integration, spoken-copy meaning, provider-rendered segment
fidelity, and final composed-video intent are reviewed or authored by the
configured multimodal roles. The server must not scan review prose for business
keywords, turn a model rejection into approval, synthesize a creative fallback
artifact, or auto-pass a semantic stage.

Deterministic code remains authoritative only for non-semantic execution
boundaries: tenant and project ownership, schema and enum validity, signed IDs
and hashes, reference ordering, file existence and dimensions, exact duration
arithmetic, idempotency, leases, queue state, retry budgets, and consistency
between a model's structured verdicts and its top-level decision. Rejection is
a completed model decision that routes to bounded repair; it is not a failed
transport and is never silently accepted.

Image failover is likewise exact-route state, not provider-wide prose state.
An enabled route inventory is loaded before circuit filtering, so a temporarily
unavailable image model cannot be resurrected through the old compatibility
key fallback. Same-provider model failover preserves completed boards and
clears only the failed route's pending work.

`VIDEO_PROMPTS` is a historical storage boundary for Director-enforced
projects. Its local compiler performs only lossless timeline clipping and
reference-index binding from the already model-authored, independently reviewed
Director artifact and Production Plan. It is not another creative author. When
provider output needs a semantic rewrite, the multimodal segment execution
Director performs that rewrite from the signed plan plus current media evidence.

Legacy `deterministic_server_fallback` review evidence is recognized only so
self-heal can invalidate it. Active execution must never create that marker.

### Recovery Supervisor is the sole route-transition writer

Provider exhaustion, browser unavailability, and temporarily empty eligible
route inventories are incidents, not permission for a caller to select a
fallback. Every such caller reports the evidence to the Recovery Supervisor;
the model selects an allowed action and a per-stage distributed lease commits
exactly one transition. A second worker must preserve an existing live
cooldown instead of overwriting it with a browser wait or another submission.

`api_available=false` and `api_configured=false` have different meanings. The
former can mean all configured accounts are cooling. In that case self-heal
keeps the browser dormant and persists an exponential API inventory probe
(60 seconds through a maximum 30-minute interval). It does not pause the
project or create a browser Slot merely because no route is eligible at that
instant.

Periodic self-heal also collapses failed chained video dependencies to a fixed
point. A synthetic `dependency_failed` task owns no further provider recovery;
all descendants become terminal and are removed from the project's pending
set even if an older video waiter was lost during deployment. This prevents a
completed or quality-draining project from polling one impossible segment
forever.

## 5. Known Structural Risks

- `content_factory_tasks.py` and `direct_browser.py` are very large and contain
  compatibility paths. Refactor only behind regression tests; do not perform a
  broad rewrite on production without staged equivalence checks.
- Legacy stage constants still coexist with the active streamlined flow.
- The worktree contains many valuable uncommitted and untracked production
  changes.
- Provider documentation and capabilities change. Verify current official docs
  before altering request shapes or limits.
- Browser UI automation remains inherently less stable than API execution.
- A full repository test pass was not established at this handoff.

## 6. Next Engineering Priorities

1. Keep API-first text/image execution and reduce browser payload/token size.
2. Continue splitting large task/browser modules into tested deterministic
   services without changing behavior.
3. Add end-to-end multi-user/multi-device tests with two bridges and parallel
   projects.
4. Add invariant dashboards for duplicate execution tokens, slot ownership,
   stale pollers, missing local files, missing per-video guides, and target
   count deficits.
5. Rerun and repair the broad backend/frontend suite in isolated environments.
