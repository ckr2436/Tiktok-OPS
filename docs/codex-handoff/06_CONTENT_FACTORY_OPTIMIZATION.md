# Content Factory Optimization Blueprint

Updated: 2026-08-11 Asia/Shanghai

## 1. Production Evidence

Project `168` was used as the first sustained 50-video production run. At the
2026-07-20 planning checkpoint it had 37 verified local MP4 files and 37 matching local
guides. All referenced files existed.

The same output required:

- 667 stage rows: 307 success, 309 superseded, 50 failed, and one paused row.
- 184 provider video-task rows, including 155 successes and 29 failed,
  superseded, duplicate, dependency-failed, or quality-rejected rows.
- 176 generated reference images, about 4.8 images per completed video.
- 66 successful CREATIVE attempts, 151 successful VISUAL_PREVIEW attempts, and
  58 successful CREATIVE_REVIEW attempts.
- Median stage times of about 80 seconds for CREATIVE, 99 seconds for
  VISUAL_PREVIEW, 52 seconds for CREATIVE_REVIEW, 18 seconds for FINAL_ASSETS,
  and 45 seconds for VIDEO_PROMPTS.

The largest avoidable cost is visual generation/review churn, followed by
creative regeneration and orchestration supersession. Download is not the
dominant stage.

Measured median queue waits were only about 3.6 seconds for creative, 0.3
seconds for creative review, and 2.1 seconds for visual preview, while median
execution times were tens of seconds. The three Hermes services are therefore
not capacity-bound. Adding another creative authority would increase context
transfer and disagreement without addressing the measured bottleneck.

A copy audit of the latest 40 successful variants found that all 40 scripts
used four spoken segments, all 40 put the product in the final segment, and all
40 mentioned the configured `$7.99` offer. However, only 5 had an explicit
causal bridge into the product and only 11 stated an expected human change.
This proves the old four-part campaign scaffold was producing a mechanically
attached conversion ending instead of one complete story.

Runtime evidence also showed that every Celery worker imports the complete
application task graph. A typical prefork child retains roughly 550-650 MB RSS,
the AI-video parent approaches 1 GB, and Celery Beat retains roughly 550 MB.
This multiplies memory by queue and concurrency even when queues are idle.

### 2026-07-20 zero-media Director canaries

Project `168` remained manually paused with zero active media stages while the
new text-only gates were exercised.

- A strict 40-second copy canary exposed and then closed three production
  contract gaps: provider-illegal segment timing, missing display-copy reading
  budgets, and missing per-segment spoken-copy budgets.
- The accepted canary used four provider-legal 10-second clips. Every clip fit
  its own 25-word spoken ceiling; the script used concrete household pain, an
  earned product preference, a bounded routine change, the confirmed `$7.99`
  price, and the exact authorized CTA.
- A 50-intent series canary used five resumable 10-intent pages. It made 16
  Director calls across three revisions and three independent whole-series
  reviews, then correctly entered a quality pause without authorizing media.
- The repeated, valid review findings were unearned product-category bridges
  and semantically repeated conversion architecture. Merely changing the
  character, prop, format label, or pain scene did not create a different
  conversion route.
- The experiment also exposed a review-contract error: a series intent is only
  a feasibility plan, so it cannot be scored as though script line IDs, final
  visual plans, and lossless line allocation already exist.

That canary exposed an efficiency defect: a whole-series rejection regenerated
all five accepted pages. The content-family implementation below closes it by
repairing only affected intents or pages and reserving the independent
whole-series pass for compact cross-series coverage and duplicate checks.

### 2026-07-21 content-family and copy canaries

Project `168` remained manually paused with zero active production stages.

- The universal profile produced a 50-intent slate organized by model-owned
  content families rather than one purchase reason or mother template per
  episode.
- Coverage required four bounded semantic versions. The Critic rejected
  cross-family caregiving duplicates, repeated within-family conflicts, and
  unconfirmed portability or compactness language before any episode or media
  work was authorized.
- The approved coverage scored 93 for semantic distinctness, 94 for conversion
  diversity, 92 for balance, and 100 for truth boundary.
- Five 10-intent pages passed local feasibility review. Four deterministic
  contract repairs corrected an intent selecting a globally true attribute
  outside its reserved territory. The final compact global review approved all
  50 intents.
- The whole run was read-only, authorized no media, and peaked around 121 MB
  RSS after removing the shadow tool's import of the full Celery task graph;
  the prior path approached 953 MB.
- Three representative 40-second copy canaries ran concurrently through the
  same Director and Critic services: a first-person story, a fact-first
  explainer, and a silent display-copy video. All three passed deterministic
  preflight and independent copy review. The story needed one explicit extra
  revision for natural American phrasing; thresholds were not reduced.
- Current provider capability compiles a 40-second artifact into four
  transport segments of 10 seconds. That is a generation constraint, not a
  four-beat story template. Product reveal, content form, audio mode, and line
  allocation remain Director-owned unless the project explicitly constrains
  them.

Coverage checkpoints now preserve the last valid Critic-reviewed candidate
when a later revision fails its contract. A resume continues the exact failed
revision instead of restarting coverage version one. Resuming after a semantic
budget pause requires an explicit higher budget; rerunning with the same
budget cannot silently create another revision.

Coverage semantic repair now uses a hash-bound delta protocol. The Director
receives the signed base coverage map, the exact Critic-cited territory IDs,
and a schema restricted to those variant and family coordinates. It returns
only changed family or territory objects. The runtime rejects stale hashes,
unknown scope, uncited variant/family changes, invented truth, duplicate
territories, and invalid counts before atomically signing the replacement map.
Unchanged pages remain byte-for-byte equivalent to the reviewed base. Focused
resume, contract-failure, stale-hash, and out-of-scope tests pass, and the
complete text-control regression is `63 passed` with no media authorization.

## 2. Hermes Role Topology

Three isolated Hermes runtimes are sufficient for the first general pipeline:

- primary Hermes: execution and operational recovery
- content Director Hermes: whole-series planning and immutable per-video copy
- content Critic Hermes: independent review against project-owned criteria

Do not add a fourth general Hermes process yet. A separate "director" is a
logical and security boundary, and that boundary already has a dedicated
runtime. Add another physical process only when measured Director queue latency
or model/context requirements cannot be isolated through the existing role.
Windows browser slots are fallback executors, not additional reasoning roles.

Operational recovery is a fourth *logical role* inside the existing primary
content-control topology, not a fourth resident Hermes service. The stateless
Recovery Supervisor receives only structured fault metadata and a
server-generated allowed-action list. It recommends API cooldown, API/browser
switching, browser wait, or pause; deterministic code retains leases,
idempotency, tenancy, cost ceilings, manual-pause authority, and the atomic
state transition. If the role is unavailable or returns an illegal action, a
safe fallback prefers API cooldown and never escapes the allowed envelope.

The role flow is:

```text
Project-owned DirectorSeriesBrief
  -> SERIES_STRATEGY: model-owned coverage map and conversion territories
  -> SERIES_PAGES: bounded intent pages against reserved territories
  -> page-level feasibility criticism and local repair
  -> compact global fingerprint criticism
  -> DIRECTOR: immutable program and complete script for one variant
  -> independent copy criticism
  -> CREATIVE: visual and production design around locked line IDs
  -> media authorization gate
```

Director and Critic must be stateless, API-only, physically isolated, and
fail-closed. Neither role may create images, submit videos, activate browser
slots, reuse another role's conversation, or change project-owned truth and
quality thresholds.

The Content Director is already the requested "director-type Hermes." It has
two logical operations, series direction and episode direction, but does not
need a fourth physical service. The Critic remains physically independent.
Additional Director or Critic processes are replicas for measured queue
latency, not new creative authorities. Add replicas only after stateless
idempotency is proven and P95 queue wait materially exceeds model execution
time.

## 3. Non-Hardcoded Contract Boundary

The generic engine must not contain a campaign's brand, product name, price,
storefront wording, hashtag set, segment count, story type, provider, model, or
queue name as business behavior.

Compile immutable `DirectorSeriesBrief`, `SeriesSlate`, per-variant
`DirectedContentArtifact`, and `ExecutionSpec` objects from saved project data:

```text
ProjectSpec
  product truth and visual invariants
  channel/distribution profile
  creative copy contract
  promotion authorization
  duration and segment plan
  provider capability requirements
  concurrency and quality budgets
```

The creative copy contract carries:

- segment count and timing
- product-free range and reveal point when explicitly requested
- conversion segment
- project product identity
- authorized price/offer and CTA semantics
- opening segments that the ending must tie back to
- copy budget and quality rules

Prompts consume these structures. Deterministic validation reads the same
structures. A new campaign changes configuration, not Python conditionals.
The server may register generic capabilities and validate their contracts, but
must not contain a list of prewritten scenes, loss stories, visual lanes,
segment copy, or campaign-specific endings.

Series intent contracts use a small generic core rather than an unbounded
creative-strategy dictionary or a commerce-shaped mother template:

```text
SeriesContentFamily
  audience tension or need
  viewer-value context
  response or action route
  permitted truth options
  planned count and differentiation mandate

SeriesIntent
  viewer moment
  content form and execution logic
  value progression
  bounded outcome or response
  truth and semantic fingerprints
```

Product conversion, strong-pain storytelling, curriculum progression,
interview evidence, news sourcing, or another genre-specific structure is an
optional project contract layered on that core. For example, a strong-pain
commerce project may require a structured pain and conversion hypothesis;
an entertainment, music-only, reporting, or visual-demonstration project must
not be forced to invent one. The Director still chooses the character,
setting, metaphor, opening, format, audio mode, visual grammar, reveal
position, and exact copy. Deterministic validation proves only the fields the
project enabled; the independent Critic judges the configured standards.

Before episode territories, the Director creates model-owned
`SeriesContentFamily` campaign jobs. A family declares its strategic job,
  audience stage, content-type space, viewer-value role, planned count, permitted
  truth options, allowed reuse, and differentiation mandate. Finite confirmed
  source truths may repeat deliberately inside a family. Episode territories
must vary viewer moment, evidence, form, or execution instead of inventing a
renamed purchase reason for every video. Product facts are required at family
level; territories inherit them unless intentionally narrowing the allowed
set.

Series-level and script-level review criteria must remain separate:

- page feasibility review checks whether each intent can plausibly support its
  declared pain, bridge, confirmed choice reason, bounded change, and truth
  boundary;
- global series review receives compact semantic fingerprints and checks
  coverage, duplicates, and conversion-route diversity;
- copy review alone scores exact American phrasing, line continuity, word
  budgets, CTA preservation, and final script coherence;
- visual review alone scores whether generated references and segment plans
  faithfully express the locked script.

No stage may score a downstream artifact that does not yet exist.

## 4. Target Runtime Model

Create first-class durable entities instead of keeping execution identity only
inside JSON:

```text
Project
  Execution
    VariantRun
      StageRun
        ReferenceRun
        SegmentRun
      Deliverable
```

Each row owns workspace, user, project, execution, variant, attempt,
idempotency key, lease, heartbeat, state, provider route, input digest, output
digest, error class, and timestamps.

Use event-driven transitions:

```text
creative.accepted
  -> references.requested
  -> references.accepted
  -> prompts.compiled
  -> segments.submitted
  -> segment.downloaded
  -> variant.composed
  -> guide.created
  -> target.reconciled
```

The periodic reconciler repairs missed events and expired leases. It must not
be the normal scheduler and must not create duplicate waiter tasks every
minute.

## 5. Queue And Worker Isolation

Split the universal Celery application into lightweight task registries:

- content control/text
- image generation/review
- video submission/poll
- download/composition
- unrelated platform workloads
- scheduler only

Semantic queue roles are configured at deployment. Providers are selected by
the capability registry, not encoded in queue names. Queue migration includes
producer routes, consumers, Beat entries, watchdog configuration, monitoring,
and bounded draining of old messages.

The expected result is that an idle content or video child does not import
Whisper, advertising, every provider SDK, and the full API graph.

## 6. Parallelism

Parallelism is budgeted by resource, not expressed as one global worker count:

- browser turns: one per project and one per pinned slot
- text creative: bounded per workspace, serialized where diversity history is
  required
- image references: parallel within one approved variant up to provider and
  workspace quotas
- image review: batch one variant's references in one structured review call
  when the model supports it
- API video variants: sliding window configured per project
- segments within a continuity chain: dependency-aware
- downloads and composition: asynchronous and independent from browser state

Whole-series work uses a two-step parallel model:

1. One Director call creates and locks a compact coverage map. The number and
   nature of content families and creative territories are model-owned within
   project constraints, not a server list of mother templates.
2. Intent pages assigned to non-overlapping reserved territories may run in
   parallel. Page Critics repair only failing intents. A compact final pass
   compares fingerprints rather than rereading every full intent and never
   causes clean pages to be regenerated.

Per-video copy may start only after its page and the global coverage map are
approved. Media remains separately gated by the immutable per-video copy
artifact.

Use database or Redis semaphores for workspace, provider account, model,
browser device, and memory class. Concurrency values are deployment data.

## 7. Quality And Repair Boundaries

Fail before expensive work:

1. deterministic schema, timing, offer, and product-reveal checks
2. semantic creative gate for concrete loss, causal bridge, continuity, and
   cross-variant diversity
3. image generation
4. pixel-grounded review
5. prompt compilation
6. video submission

Repair the smallest failed object. One bad reference regenerates one reference;
one bad segment replaces one segment task generation; a copy failure creates a
new creative attempt without superseding completed variants.

Separate user-facing deliverable ordinals from internal variant attempts. A
failed variant may leave an audit trail but must not create gaps or apparent
deletions in the user's video list.

## 8. Observability And Acceptance

Add metrics and invariant checks for:

- accepted variants per creative attempt
- images per accepted variant
- review repairs per reference
- provider tasks per delivered segment
- time in queue, provider, download, compose, and guide stages
- duplicate/superseded task rate
- missing local file or guide
- browser awakenings during API-only work
- stale leases, waiters, and legacy queues
- RSS per worker role and imported task registry

The optimized pipeline is accepted only when a sustained run shows:

- requested local MP4 and guide counts match exactly
- zero browser processes during API-only stages
- zero messages in retired queues
- no duplicate logical provider submissions
- bounded repair attempts with no project-wide cleanup for local failures
- materially lower stage supersession and provider-task overhead
- worker memory proportional to its task role rather than the whole platform

## 9. Migration Order

1. Whole-series Director slate, per-variant immutable copy, and pre-expense
   authorization gates.
2. Split series planning into a model-owned coverage map, page-local
   feasibility review, and compact global fingerprint review.
3. Add project-selectable structured intent contracts, including pain and
   conversion hypotheses when the project requires them, and repair only
   rejected intents/pages.
4. Remove the old hardcoded diversity lanes after the new canaries pass.
5. Queue/watchdog source-of-truth cleanup and legacy-message drain.
6. First-class VariantRun and SegmentRun tables with dual-write validation.
7. Event outbox and idempotent transition consumers.
8. Role-specific Celery applications and scheduler registry.
9. Visual batch review and resource semaphores.
10. Read-path cutover, old JSON compatibility retirement, and invariant
   dashboard.

Do not combine all phases into one production rewrite. Each phase requires
shadow comparison, focused regression tests, one controlled project rollout,
and a rollback boundary.

## 10. Implemented Safe First Slice (2026-07-23)

The first deployable slice deliberately avoids the larger persistence and
worker-registry migration:

- API-only AI producer intake with explicit digest-bound confirmation
- no implicit default product binding and visible proposal invalidation when
  the product selection changes
- model-authored per-beat continuity dependencies compiled into provider
  segment transport
- dependency-aware parallel submission for independent segments
- local-download segment quality gate before a dependent successor is
  released
- deterministic impossible package-state rejection before media spend
- user-facing project status wording and a responsive single-column mobile
  layout
- provider-task and result-file recovery queries reassert workspace and user
  ownership even when task ids came from a persisted project ledger
- browser-local drafts and producer sessions are keyed by workspace plus user;
  legacy workspace-only cache keys are removed instead of inherited
- upload byte limits are enforced while streaming, so an absent or false
  client-declared size cannot fill the shared content volume
- project deletion is a durable two-phase tombstone: the project disappears
  from user/admin reads first, local-only provider work is cancelled, already
  submitted work drains, and periodic self-heal retries physical cleanup
- terminal `COMPLETE` projects are normalized out of impossible
  `waiting_bridge` states without deleting user data or acquiring a browser
- asset downloads resolve only inside the content repository, including when
  a persisted path is stale or tampered with
- provider drain logic treats superseded, cancelled, and downloaded rows as
  terminal, so retired variants cannot strand waiters or deletion tombstones
- content-factory tests redirect both SQL and RAID storage to disposable paths;
  regression runs no longer create production `cf_*` directories

The existing Director, copy artifact, variant execution ledger, provider task,
download, composition, and quality-pause authorities remain in place. No ads
code or ad worker route is part of this slice.

## 11. Current Creative Intent Execution Slice (2026-08-11)

The default intake now uses `content_producer_v19_fast_product_grounding` as the only
member project-creation path:

- Natural conversation is compiled into a signed, evidence-grounded
  `CreativeIntentManifest` plus ordered `DeliverableSpec` rows rather than a
  campaign mother template or a manual parameter form.
- Every requirement carries a stable ID, original quote, professional
  interpretation, observable checks, creative freedom and forbidden reuse.
  Multiple requested outputs are never assumed to reuse one script merely
  because count is above one.
- Multi-script packages are duration-checked per deliverable and stored as
  hash-bound `required_verbatim_voiceovers`; the whole package is not measured
  as one video or copied into every variant.
- The current versioned script takes precedence over its original source row.
- Proposal confirmation binds the parameter proposal, effective intent, current
  script hash, and pending-decision identity.
- The frontend shows the effective goal, requirement evidence and observable
  checks, each output's full locked script when present, and the downstream
  execution lineage.
- Exact conversational confirmation uses the displayed pending decision and
  does not enter another model confirmation loop.

During series materialization, each distinct locked script is attached only to
its own Director brief. Director mappings and Production Plan mappings make
every critical/high requirement traceable to concrete execution coordinates;
the segment reviewer and final composed-output guardian cite those same IDs.
The Director and Critic still own all creative choices the user left open.
Deterministic code enforces evidence integrity, duration, tenancy,
confirmation, count, and execution safety rather than prescribing a story.
