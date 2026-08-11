# Hermes Content Factory

## 1. Active Production Workflow

The active variant flow is:

```text
PRODUCT LIBRARY FACTS (only when product facts are created/refreshed)
  -> CREATIVE
  -> VISUAL_PREVIEW
  -> CREATIVE_REVIEW
  -> FINAL_ASSETS
  -> VIDEO_PROMPTS
  -> asynchronous provider generation/download/composition
  -> EDIT_PACKAGE per completed video
  -> target-count reconciliation
  -> COMPLETE or bounded automatic quality pause
```

Legacy constants such as `QA`, `VIDEO_QA`, visual approval, and video approval
may remain for compatibility, old rows, or restart mappings. They are not gates
in the intended production variant flow and must not be reactivated casually.

Core implementation:

- `backend/app/services/hermes_agent/content_factory.py`
- `backend/app/services/hermes_agent/content_factory_api.py`
- `backend/app/services/hermes_agent/stage_routing.py`
- `backend/app/tasks/hermes_agent/content_factory_tasks.py`
- `backend/app/services/hermes_agent/direct_browser.py`
- `backend/app/features/tenants/hermes_agent/router.py`

## 2. Product Library

The product library is company-scoped and managed on its own tab.

- Users upload product facts, package images, ingredient images, brand
  guardrails, claims, and supporting PDFs.
- Upload completion triggers product-fact analysis.
- Product facts are stored and reused. A content project selects an existing
  product and normally starts at `CREATIVE`.
- Product facts are rerun only when the user explicitly refreshes them or
  updates source material.
- Product assets can be updated and deleted.
- Promotional information is project-specific, not immutable product fact.
- Product selection is optional. Entertainment-only projects may run without
  a product.

Uploaded asset roles matter. Ingredient/supplement-facts images are evidence,
not automatic visual anchors. The authoritative package image is a product
anchor. Character images are character anchors.

## 3. Project Intake

The only member-facing project creation path is the AI Producer conversation.
There is no parallel manual/advanced create form and no direct project-create
HTTP route. The Producer collects only information that is still needed,
explains its interpretation, and asks for one digest-bound confirmation before
creating work.

The conversation may establish project name, optional product-library item,
deliverable count, duration strategy, model preference, resolution, language,
aspect/reference mode, benchmark evidence, character anchors, and execution
preferences. These values are not a second creative authority. The signed
`CreativeIntentManifest` described below is the semantic source of truth.

Project state is persistent. Editing or restarting a project must reload the
saved values, assets, prompts, and selected device rather than initializing
defaults.

## 4. Benchmark Video Semantics

When a benchmark video is uploaded:

1. Store the video locally.
2. Extract subtitle/transcript content.
3. Extract ordered keyframes/contact sheets with FFmpeg and the video-analysis
   pipeline.
4. Persist transcript and frame assets with benchmark roles.
5. Pass only the relevant compact benchmark evidence to `CREATIVE`.

Interpret the user's requirement:

- Explicit "1:1 copy/replicate" means preserve the benchmark's transcript and
  structural scene/style timing as closely as allowed, adapting only the
  product, characters, and confirmed requirements.
- "Imitate/reference" means rewrite the copy and vary scenes while retaining
  the useful format and pacing.
- No benchmark means create meaningfully different creative concepts across
  variants.

Do not send an unrelated contact sheet or a whole project asset dump to every
stage.

## 5. Creative Stage Owns The Whole Video

`CREATIVE` plans one complete video at a time. It owns:

- complete story arc
- hook, middle proof/value, and conversion ending
- full spoken script or voice-over
- voice gender, identity, tone, and language continuity
- total duration and segment boundaries
- exact per-segment dialogue allocation
- visual reference-board plan and required panel count
- promotion/price/CTA requirements confirmed by the project

For 20, 30, 40, or 50 second videos, the creative plan must remain a coherent
single story. Segment boundaries must not split a sentence or leave one
segment empty. Allow practical editing headroom because the final human edit
may be shorter than raw generated duration.

When project requirements include price, promotion, product value, or CTA, the
back half must naturally speak them. The CTA is feed-native spoken language
such as clicking the yellow cart below, not a landing page, QR code, fake
shopping UI, "tap to shop" graphic, or TV-shopping presentation.

Different variants must use different hooks, story situations, actions,
dialogue, and conversion framing. Merely changing a version name is not enough.

## 6. Visual Preview

`VISUAL_PREVIEW` creates one or more ordered portrait reference boards.

- The panel count comes from the approved creative plan, not a fixed 2x3 grid.
- A long video may need multiple boards. Preserve global panel order.
- Boards and panels should match the project's aspect ratio.
- Uploaded character anchors and descriptions may guide identity generation.
- Generated boards may show the real product in a story scene when needed.
- The AI must not invent a new package or generate a separate white-background
  product anchor. The user's uploaded package remains authoritative.
- The output must be a newly generated assistant image. Never accept an
  uploaded product, ingredient image, benchmark frame, or old project image as
  the visual preview.

API image generation is preferred. The browser is a fallback. Do not use
private/custom GPTs; use standard ChatGPT plus Hermes-owned stage instructions
when browser execution is required.

The active API route is chosen from platform-managed
`content_factory_visual` image-route priorities. A provider submit returns a
durable task id before polling, or a synchronous inline image for a route that
explicitly declares that response contract. The stage persists the normalized
result and reuses it after worker restarts. Completed remote or inline objects
must be decoded, validated, and stored in the project RAID directory before
the visual reference is successful. Route health and failure history are
tracked by exact `provider:model`; a circuit-open model may not re-enter via a
legacy provider-only credential fallback.

## 7. Creative Review

`CREATIVE_REVIEW` validates the generated board against the creative plan:

- correct story and characters
- exact required panel count
- correct panel order
- visual relevance
- no unrelated images
- product use is plausible
- no invented product anchor
- approved for splitting

The review may not approve a mismatched panel count simply to advance the
pipeline. If creative requires seven references and the board has six, repair
the visual board. The creative plan is authoritative unless the user changes
it.

## 8. Final Asset Splitting

`FINAL_ASSETS` is normally a deterministic local stage:

- Read only the verified visual-preview board or boards.
- Detect actual divider lines and panel rectangles.
- Split in stable reading order.
- Validate expected count, minimum dimensions, aspect, blank borders,
  duplicate panels, and usable visual content.
- Assign semantic roles and `reference_index`.
- If local splitting is unsafe, request a visual repair or bounded image-tool
  fallback. Do not silently use equal-size crops that cut across panels.

Never split the uploaded product image, supplement facts, character anchor, or
benchmark contact sheet as if it were the generated preview.

## 9. Reference Semantics

References and text have separate responsibilities:

- character anchor: identity, face, hair, body, wardrobe
- scene anchor: room geometry, lighting, camera side, stable props
- action anchor: the current segment's key pose/action
- product anchor: exact uploaded package geometry, label, cap, and proportions
- first-frame anchor: literal continuity from the previous segment

Only references relevant to the current segment are uploaded. Do not attach all
final assets to every segment merely because the provider permits seven.

For multi-segment continuity, later segments should use the previous segment's
last frame as the first and strongest continuity anchor. Other references are
action/product aids and must not replace the established identity or scene.

## 10. Video Prompt Contract

`VIDEO_PROMPTS` is a structured compiler boundary. It must produce one prompt
per segment and the current deployed compiler version must be preserved unless
explicitly migrated.

Each provider prompt contains only:

- current segment goal
- segment-local timeline with time ranges
- current actions
- current camera movement and pace
- exact dialogue/voice-over for this segment
- transition in/out and continuity state
- compact consistency constraints
- compact negative constraints
- authoritative `@imageN` bindings matching upload order
- output aspect, resolution, duration, and language

It must not include:

- the entire project JSON
- unrelated product facts and PDFs
- another segment's full plot
- duplicate/conflicting CTA text
- Python/JSON fragments accidentally embedded in dialogue
- prose describing the whole video
- repeated reference-role paragraphs that add no provider value

Creative owns the full script. The compiler must not invent a different voice,
repeat the full story in every segment, or move CTA dialogue into the hook
segment.

## 11. Duration And Composition

The selected model determines allowed segment duration and reference modes.

- Omni Flash provider routes currently use supported 8/10 second clips where
  applicable.
- Seedance 2.0 Mini supports model-specific 1-15 second duration and
  resolution/reference constraints.
- A complete 20 second video may be two 10 second segments.
- Longer videos use the minimum coherent number of allowed clips.

Each segment is a continuation, not a miniature full video. Segment prompts
must end at their boundary. When all segments for one variant are locally
available, compose that variant immediately; do not wait for other variants.

Use stable names based on project, version, and complete-video index.

## 12. Browser-Serial, API-Parallel Variants And Target Count

Variants run with one browser-owned creative turn at a time:

```text
variant 1: creative -> references -> prompts -> submit video
variant 2: create a different creative -> references -> prompts -> submit
...
```

Do not generate ten near-identical creative plans at once. The provider queue
and local download/composition are asynchronous and may overlap with the next
creative variant, but a project must never have two browser stages in flight.

`max_api_video_variants_in_flight` is the per-project provider cap. It defaults
to `1` (serial compatibility) and is bounded to `1..4`. A controlled rollout
may set it to `2`: after one variant has durably submitted API video tasks, the
next browser turn may start; downloads and composition then replenish the
provider window as each local MP4 is verified. Failed variants remain in the
window until their idempotent retry or variant-scoped recovery is resolved.
Never use this setting to run browser turns concurrently or to delete another
in-flight variant's task, references, assets, or edit guidance.

The target count is complete videos. Reconcile:

- completed variants
- submitted variants
- failed variants
- variants needing replacement
- per-video edit guides

Clean failed variant outputs so they cannot pollute later reference selection.
Generate replacements until the target is met or a bounded quality rule pauses
the project.

## 13. Download, Completion, And Edit Guidance

Provider completion is not local completion.

1. Poll with a lease/heartbeat and one live poll owner.
2. Download on the server through an asynchronous queue.
3. Retry transient download failures.
4. Preserve the remote URL for user fallback when local download exhausts.
5. Mark the segment successful only after the local file is verified.
6. Compose a complete video as soon as that variant's segments are ready.
7. Generate one edit guide for that complete video.

The edit guide is for a human editor. It contains:

- concise publish title
- up to five relevant hashtags
- chapter overlay titles tied to the actual segment timing and story beat

It must not contain internal reference bindings, JSON fragments, provider
instructions, generic "hook title" placeholders, or unrelated ad-operations
advice. If the associated video fails and is removed, remove its guide.

## 14. Pause, Resume, And Recovery

Manual and automatic pause metadata are distinct:

- Manual pause is an explicit user action.
- Automatic quality pause is a bounded system decision after repeated
  irreparable creative/visual/provider quality failures.
- During automatic quality pause, drain already-submitted video downloads,
  composition, and edit guides.
- Do not start a new creative variant while draining.
- A later manual pause remains authoritative.
- Resume clears obsolete pause ownership metadata and starts from the latest
  verified checkpoint.

Recovery must use execution leases, heartbeats, project/variant/stage
idempotency, and output provenance. It must first inspect for a late ChatGPT or
provider result before resending.

Operational transport and browser-capability failures pass through the
stateless `content_recovery_supervisor` logical role. The server supplies a
bounded action envelope such as cooled API retry, API/browser switch, browser
wait, or non-retryable pause. The model cannot submit work or mutate state;
the deterministic executor validates its choice and performs one atomic
transition. Browser upload-zero, login-required, and offline incidents return
to an available API route or wait without reopening Chrome in a hot loop.
Quota, authentication, explicit prompt-policy failures, manual pause, and the
configured recovery-cycle ceiling remain deterministic fail-closed guards.
Every decision is stored as bounded stage/project audit metadata.

## 15. AI Producer Intake And Segment Release Gate

The Producer accepts a supported public TikTok, Douyin, Kuaishou, YouTube, or
Facebook benchmark URL directly in the user turn. The API validates the HTTPS
host and public DNS boundary before creating a tenant/user-scoped attachment;
the Whisper queue then uses `yt-dlp` asynchronously with redirect and extracted
media URL validation, a 200 MB ceiling, bounded retries, and optional
site-cookie support. It extracts timed speech and renders chronological full
coverage contact sheets. A routed multimodal model must analyze the opening
hook sequence, story progression, pacing/edit grammar, product/conversion
entry, transferable mechanisms, and elements that must not be copied. One
preview frame or transcript-only reasoning is not a completed benchmark
analysis.

The original URL turn is persisted with an idempotent Producer turn id. After
multimodal analysis succeeds, a Hermes queue task resumes that exact turn even
if the browser refreshed or disconnected. The Producer discusses the evidence
and any unclear transfer choices with the user; project creation still needs
the ordinary explicit confirmation. A newer benchmark deactivates the older
one for the current requirement without deleting audit history. The source
video is analysis evidence only and is not sent to a video provider unless the
user explicitly requests and confirms a supported video-to-video workflow.

The default project entry is an API-only AI producer conversation. It converts
plain-language user intent into a reviewable proposal for count, duration,
platform, audience, visual style, pacing, audio identity, and conversion
direction. It is a logical role over the existing text gateway, not another
browser or long-lived worker process.

The producer cannot create a project, start a browser, or authorize media. A
separate confirmation request must present the exact saved proposal SHA-256.
Conversation and confirmation reads are scoped by workspace and user, lock the
conversation row, and idempotently create at most one project. Product facts
remain company-library owned. Promotion evidence is accepted only as a
verbatim user quote, and changing the selected product invalidates the
displayed proposal.

Producer prompt v19 persists an effective `IntentSpec` containing one signed
`CreativeIntentManifest` beside the parameter proposal. Every requirement has
a stable `R-NNN` identity, verbatim user/attachment evidence, semantic
interpretation, observable checks, permitted creative freedom, forbidden
reuse, priority, scope, optional deliverable ordinals, and an optional time
window. The manifest is tamper-evident and confirmation refuses an absent or
unsigned manifest. A multi-script source package therefore becomes one
hash-bound script per deliverable; target count alone never selects same-copy
visual-variant behavior. The immutable original source remains audit evidence,
the latest authorized script version is the working authority, and
confirmation creates one immutable execution snapshot.

The manifest is an evidence contract, not a fixed story template. It states
what the viewer must be able to observe while leaving premise, setting,
characters, visual metaphor, and shot design open unless the user constrained
them. A benchmark request such as "keep the hook strength" may not be reduced
to a `fast_opening=true` flag or a minimum action count.

The member UI renders the effective interpretation and every deliverable before
confirmation. Exact natural-language confirmation commands are bound to the
currently displayed pending-decision digest and call the same idempotent
confirmation endpoint as the button; they do not ask the model to interpret
"confirm" again.

After confirmation, the universal Director must map every critical/high
requirement to concrete script lines, capability nodes and provider segments.
The Production Plan must map the same requirement to signed beats, references,
audio cues and/or copy lines. Compilation attaches the resulting requirement
contract only to the segments that own it. Segment review cites failed
requirement IDs and evidence; it does not substitute generic checks such as
"enough actions" for the user's requested mechanism.

After local composition, an independent final intent guardian reviews a
densely sampled opening plus later chronological frames against the original
requirements and, when supplied, the benchmark evidence. Originality and
effectiveness transfer are separate judgments. A localized failure regenerates
only the mapped segment; an ambiguous or whole-program failure returns to the
Director or enters the bounded quality pause. A video cannot count toward the
target merely because every provider segment downloaded successfully.

Source-based optimization requests also carry an AI-authored
`source_transformation_contract`. It records source fidelity, protected user
requirements, only the authorized changes and time windows, remaining creative
freedom, observable success checks, and the compatible execution strategy.
This contract is passed into the universal Director before its brief is
compiled. Project-owned critics score source-boundary fidelity at 100 as a
blocking criterion; an internally coherent rewrite is not evidence of user
authorization. A `local_edit` contract is fail-closed at the provider task
boundary, and exact/bounded outputs compare duration, decoded audio, and sampled
frames outside authorized windows before they can count as completed media.
For an AI remake, the same contract separates semantic transfer from media
reuse. Preserving hook logic, beat order, pacing, shot rhythm, or conversion
structure does not authorize copying source pixels, actors, voices, audio,
embedded captions, platform UI, watermarks, or watermark-covering bands. A
`source_media_reuse=forbidden` result must pass the source-originality gate:
decoded audio may not match and normalized frame samples may not reveal copied
source media.

Production Plan beats explicitly declare whether provider transport needs the
previous segment or is independent. Independent segments may submit in the
same API wave; dependent segments are released only after their predecessor is
downloaded locally and passes the segment release quality gate. That gate
checks duration, dimensions/aspect, required audio, and product visual review
when a product anchor is required. A failed segment cannot donate a continuity
frame to later work.

Deterministic Production Plan validation also rejects impossible physical
package state transitions, such as contents leaving a sealed bottle without an
opening action, before image or video spend.

## 16. Deletion And Local Storage Boundaries

Project deletion uses a durable tombstone rather than trying to combine a
database transaction with recursive filesystem removal. The tombstone is
hidden from member and admin project reads, releases its browser lease,
cancels local-only provider work, and blocks late stage or waiter deliveries
from reviving the project. Already-submitted provider work is allowed to drain;
then the route or periodic self-heal removes the three project-owned storage
trees and finally deletes the database row. Cleanup errors remain retryable and
must never be swallowed as a successful physical deletion.

Provider states such as `superseded`, `cancelled`, and `downloaded` are terminal
for drain and deletion purposes. They must not keep a hidden tombstone or a
global video waiter alive indefinitely.

The content repository defaults to `/data/gmv_ops/hermes_content_factory` and
is configurable through `CONTENT_FACTORY_STORAGE_ROOT`. Test processes must
set that variable to a disposable directory before importing content-factory
services; an isolated SQL database alone is not sufficient filesystem
isolation.

Every provider task and result-file recovery query reasserts workspace and user
ownership even when a globally unique task id was read from project JSON.
Browser-local drafts and producer session keys include both workspace and user.
Uploads enforce byte limits on the received stream, not only the optional
client size field. Asset downloads resolve symlinks and are served only when
the final path remains inside the content repository.

`waiting_bridge` is an operational wait state, not a retention policy. It is
never automatically deleted. A project whose authoritative stage is already
`COMPLETE` is normalized back to `complete`; other waiting projects remain
user-owned until explicitly resumed, paused, or deleted.
