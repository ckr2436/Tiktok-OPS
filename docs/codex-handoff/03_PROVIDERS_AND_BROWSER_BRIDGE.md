# AI Providers And Browser Bridge

## 1. Model-Based Routing

The frontend exposes models, not provider-branded duplicate pages. Providers
are redundant implementations selected by deployed capability, model switch,
active key, request compatibility, and priority.

Canonical registry:

`backend/app/services/ai_video/accounts.py`

Current catalog:

| Provider | Capabilities | Primary video model | Default priority | Reference limit |
| --- | --- | --- | ---: | ---: |
| `sub2api` | text, image, multimodal, video | `omni_flash` | 1 | 7 |
| `flow2api` | image | no video route | image route 5 | image-model-defined |
| `bandianwa` | image, video | `omni_flash` | 10 | 7 |
| `google-gemini` | text, image, video | `omni_flash` | 1000 | 7 |
| `kyy` | video | `omni_flash` | 30 | 5 |
| `toapis` | text, image, video | `omni_flash` | 40 | 3 |
| `doubao` | video | `seedance_2_0_mini` | 10 | 10 |
| `volcengine` | text, image, video | `seedance_2_0_mini` | 10 | 9 |
| `openrouter` | text, image, multimodal | no current video route | n/a | n/a |

Seedance limits are provider-specific: the self-hosted Doubao adapter accepts
up to ten ordered references, while the Volcengine route advertises nine.
Verify the deployed adapter before changing either capability.

`toapis/omni_flash` is disabled by default because it accepts only three
reference images. A platform administrator can enable or disable individual
provider/model routes. A disabled key or model route must never be selected
manually or automatically.

Key creation asks for provider and credential. Model scope and default
priorities come from deployed code. Do not let an administrator select a model
that the selected provider adapter does not implement.

## 2. Current Models

User-facing models:

- `omni_flash`
- `seedance_2_0_mini`

The registry is extensible. Add a model through:

1. canonical model ID and aliases
2. provider capabilities
3. request validation
4. provider adapter
5. routing priority and switch
6. frontend model controls
7. focused adapter/routing tests

Do not reintroduce KIE Sora 2 pages or provider-specific duplicate model tabs.

### Self-hosted Doubao Seedance pool

- `doubao` is an independent provider in the unified `AiModelRoute` router;
  it is not routed through KIE.
- Historical `KieTask`/`KieApiKey` class and table names remain only as the
  shared video-task ledger and provider identity store used by all adapters.
  They do not own provider selection.
- Each account owns one immutable Hermes browser Profile and one fixed proxy.
  Adding an account allocates a new Profile instead of replacing another
  account's cookies.
- Browser I/O is reserved for explicit login/re-login, human CAPTCHA, manual
  capability checks and a leased production submission. The scheduled
  six-hour authentication probe is HTTP-only and calls the same
  `/passport/account/info/v2/` endpoint used by the current Doubao web app.
  Only an explicit success envelope with a non-empty account identifier proves
  authentication; the probe must not open Chrome or spend quota.
- Account health is layered. `authentication` proves a fresh login,
  `network` records reachability/region restrictions, `capability` proves the
  Seedance editor, `capacity` records observed quota state, and `pool.status`
  is the final routing verdict. An HTTP 200 alone is never a login verdict.
- One account owns at most one live generation lease. Transient failures cool
  down that account; login/CAPTCHA failures disable it and allow unified route
  failover.
- Eligible accounts are ranked by secret-free production evidence: recent
  completed-video success, authentication/capability freshness, consecutive
  errors and accepted-submit latency. Pure LRU is only the final fairness
  tie-breaker. Each logical task records a bounded account-attempt timeline
  (`selecting_account`, Profile readiness, composer readiness, submit and
  remote acceptance) so slow phases and account rotation are auditable without
  exposing cookies or account details to tenant users.
- Accepted remote conversations remain bound to their submitting account and
  are polled on the dedicated `gmv.tasks.ai_video.browser_poll` lane. A
  confirmed empty or text-only conversation closes that remote identity,
  excludes the account for the current logical retry round, and republishes a
  fresh submit through the browser production lane.
- Browser submission is additionally serialized per network exit. Accounts
  may share one fixed proxy, but only one account on that proxy may type,
  upload and press Send at a time. Once Doubao accepts the request, the short
  browser marker is released while remote polling/download continues
  asynchronously under the account lease.
- A provider browser marker is valid only while its exact task lease has a
  future expiry. Expired or orphaned markers are cleared and must never reopen
  Chrome. Prompt input uses real key events and is submitted only after the
  controlled editor has retained the complete text across stable reads.
- The verified transport currently supports text/reference-image-to-video,
  9:16/16:9/1:1 and up to ten ordered reference images. Free accounts are
  bounded to 4-10 seconds; only an operator-confirmed enhanced account may
  advertise 11-15 seconds.
- The adapter resolves the original generated media resource and then uses the
  standard asynchronous RAID download. If only the edited dynamic-watermark
  delivery is available, it fails closed and lets unified routing retry or
  fail over.

## 3. Bandianwa Omni Flash Contract

The current upstream contract previously confirmed by the provider is:

```text
POST {base_url}/v1/videos?async=true
Authorization: Bearer <api_key>
Prefer: respond-async
```

Text-to-video JSON:

```json
{
  "model": "omni_flash",
  "prompt": "segment-only prompt",
  "size": "1080x1920",
  "seconds": "8",
  "input_reference": "[]",
  "generate_audio": true
}
```

Image/reference mode uses multipart form data and repeated
`input_reference[]` fields. `seconds` must be a string (`"8"` or `"10"`), not
a JSON number. Supported size mapping:

```text
16:9 -> 1920x1080
9:16 -> 1080x1920
1:1  -> 1080x1080
```

Poll `/v1/videos/{task_id}?async=true`, then the non-async fallback, then
`/content` if completion has no URL. Treat generic/public provider errors as
bounded retry candidates with a materially repaired segment prompt or
reference set. Repeating the exact rejected input indefinitely is forbidden.

## 4. Retry And Task Identity

- One logical segment has one local task identity.
- Retries update that task's attempt state; they do not create multiple visible
  tasks for the same segment.
- Submission is idempotent by project, variant, segment, and generation
  version.
- Provider polling has a claim owner and heartbeat before network calls.
- Stale recovery must not mark a live poller orphaned.
- Automatic-quality-paused projects still allow already-submitted tasks to
  finish.
- Quota/credit failures are surfaced distinctly from transient errors.
- Provider failover must respect reference limits, reference-video support,
  duration, resolution, aspect ratio, active model switches, and user-selected
  provider constraints.

## 5. Image Generation

API image generation is preferred to browser ChatGPT. Content Factory image
providers are selected from enabled, verified `AiModelRoute` rows for workload
`content_factory_visual`; the default materialized order is Sub2API
`gpt-image-2`, Flow2API `nano_banana_pro`, then Bandianwa `gpt-image-2`.
Sub2API uses `POST /v1/images/generations/async` for text-only renders,
`POST /v1/images/edits/async` for reference-guided renders, and durable task
polling. The gateway offloads completed images to a private local MinIO bucket;
the Content Factory still downloads the result to its project-owned RAID path
before marking a reference complete.

Flow2API exposes the `nano_banana_pro` logical model through its self-hosted
Gemini account pool. Its OpenAI-compatible chat adapter is synchronous and
returns an image URL or inline image data. It is eligible only when the exact
`flow2api:nano_banana_pro` route is enabled, verified, and outside its circuit
window. The Content Factory may fail over from Sub2API `gpt-image-2` to that
route without confusing the two providers or their circuit state; Flow2API
owns the Gemini account choice.

Every Sub2API submit carries a stable idempotency key and canonical request
fingerprint. Duplicate deliveries return the original task id and never launch
a second upstream render. Provider-specific retry state is preserved per board;
only after a provider's bounded budget or explicit balance failure is recorded
does routing advance to the next enabled route. Browser fallback is legal only
when no configured image API remains.

Send only stage-relevant inputs:

- compact creative visual brief
- required panel count/order
- relevant product package image
- character anchors and descriptions when provided
- essential scene/style constraints

Do not send the entire project packet, every PDF, old boards, old final assets,
or unrelated ingredient images to image generation.

## 6. Bridge Agent

Source:

- `hermes-bridge-agent/main.go`
- `hermes-bridge-agent/main_test.go`

Deployed Windows binary:

`backend/assets/MYUPONA-HermesBridge.exe`

Build it with `hermes-bridge-agent/build_windows_agent.sh`; the script emits a
Windows GUI-subsystem executable so no console window is shown.

The agent runs once per user device and:

- identifies the bound workspace/user/device
- maintains a heartbeat
- synchronizes project upload assets to a local Windows inbox
- launches and monitors Chrome profiles/remote debugging slots on demand
- connects dynamic reverse tunnels to the server
- reports slot/CDP health
- recovers Chrome/CDP after an accidental close when work still needs it
- avoids an endless reopen loop after shutdown or when no project needs CDP
- supports an explicit `dormant` acknowledgement: API-only video waits stop
  Chrome/SSH once while retaining the project's exact profile identity
- compares its heartbeat version with the server-required version and silently
  downloads, replaces, and restarts itself when they differ

Flow account onboarding uses a two-phase browser lifecycle on one dedicated,
stable profile. The first phase opens a normal Chrome without CDP or remote
debugging so the user can complete Google's own login and verification. After
the user confirms Flow is accessible and closes that window, the server moves
the same profile into a short CDP capture phase, forwards only the allowlisted
Flow session material and browser fingerprint to the local Flow gateway, and
then closes the browser. Never ask users to sign in to Google from a
remote-debugging Chrome; Google rejects that browser context as unsafe.

Flow authorization maintenance is a separate HTTP-only lifecycle. A successful
browser capture atomically enables persistent keepalive for that verified
account and fixed Profile. The local Flow gateway refreshes the short-lived
access grant every 20 minutes, ahead of the roughly one-hour expiry window.
Only accounts that are administrator-enabled, unbanned, and authorization-valid
are routable. `GRANT_EXPIRED` and `ST_REVOKED` are fail-closed routing states;
they do not mean the Google web session is logged out. HTTP maintenance never
opens Chrome. After HTTP recovery reports `GRANT_EXPIRED`, Hermes may perform
exactly one headless capture from the account's immutable Windows Profile for
that continuous grant episode. It must not open a visible login window. A
missing/invalid web session, account challenge, capture timeout, or repeated
grant rejection becomes `human_required` without another automatic attempt.
Healthy upstream authorization resets the one-shot budget for a later episode.

The frontend provides binding, selection, unbinding, slot preparation/removal,
download, update, inbox, and heartbeat endpoints under:

`/api/v1/tenants/{workspace}/hermes-agent/content-factory/bridge`

## 7. Slot Invariants

- One project, one fixed slot.
- One live stage executor per project/variant/stage.
- One ChatGPT conversation submission per execution token.
- A reconnect reuses the project's slot when valid; it must not create a new
  bridge merely because CDP briefly disconnected.
- Idle slots belong only to their device and may be selected for another
  project on that same device.
- Slots are created on demand and bounded by device/server load.
- Paused/deleted/completed projects release slots.
- API-only video stages may put their pinned slot into `dormant`: no Chrome or
  tunnel remains running, but the profile is not eligible for another project.
  A browser fallback explicitly wakes that same slot; `dormant` is never
  treated as a device outage or a generic free slot.

## 8. Windows File Upload

Chrome runs on Windows. Before `DOM.setFileInputFiles`:

1. Server places authorized project files in the workspace/project inbox.
2. The selected user's bridge downloads them.
3. The bridge reports the device-local Windows path.
4. The stage waits for a verified sync marker.
5. CDP uploads only those verified local files.

Never send `/data/...` Linux paths to a Windows browser. Never use a global
clipboard for multi-user file transport. Never reuse another project's inbox.

## 9. ChatGPT Response Capture

Browser execution must:

- use standard ChatGPT, not private/custom GPT URLs
- record the exact conversation URL and execution token
- detect a late completed response before retrying
- accept only assistant output created after the current user message
- capture generated image media, not an uploaded thumbnail
- validate project/stage JSON for text stages
- avoid opening two conversations for one stage
- avoid refreshing and blindly resending when the prior response exists

## 10. Popups, Rate Limits, And Human Pacing

Known UI blockers include:

- Chinese or English "requests too frequent" modal
- "Got it" / "Understood" buttons
- promotion/upgrade banners with a close button
- Chrome restore-page bubble
- stale overlays and composer dialogs

Popup detection must use semantic text, roles, buttons, close controls, and
visual/DOM fallbacks in both Chinese and English. It should dismiss safe,
reversible overlays before declaring the stage failed.

Rate limiting is project-local:

- dismiss the modal when possible
- persist the observed cooldown for that project/account/slot
- use adaptive backoff based on observed recovery, not one global fixed delay
- poll calmly for recovery
- inspect for the completed response before resubmission
- add small randomized, human-like interaction delays
- never use pacing to bypass security controls or evade account protections

One project's learned wait must not contaminate another project's state.
