# AI Providers And Browser Bridge

## 1. Model-Based Routing

The frontend exposes models, not provider-branded duplicate pages. Providers
are redundant implementations selected by deployed capability, model switch,
active key, request compatibility, and priority.

Canonical registry:

`backend/app/services/kie_api/accounts.py`

Current catalog:

| Provider | Capabilities | Primary video model | Default priority | Reference limit |
| --- | --- | --- | ---: | ---: |
| `bandianwa` | image, video | `omni_flash` | 10 | 7 |
| `google-gemini` | text, image, video | `omni_flash` | 20 | 7 |
| `kyy` | video | `omni_flash` | 30 | 5 |
| `toapis` | text, image, video | `omni_flash` | 40 | 3 |
| `volcengine` | text, image, video | `seedance_2_0_mini` | 10 | code-defined |
| `openrouter` | text, image, multimodal | no current video route | n/a | n/a |

The current code reports a Seedance reference limit of 9; verify official
capabilities and the deployed adapter before changing it.

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

API image generation is preferred to browser ChatGPT. Bandianwa
`gpt-image-2` is the current intended image route when an active compatible key
exists.

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
