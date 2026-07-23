# Universal Content Showrunner Plan

Status: architecture and zero-media contracts implemented; production project
`cf_008ac09e8d1b4e33b781` remains manually paused before the two-variant
production canary.

Current production checkpoint (2026-07-21):

- 37 durable local videos and 37 per-video guides;
- no active content stage and no browser lease;
- project pipeline mode `bounded_api_parallel_v1`;
- provider-variant window `2`;
- release manifest limited to variants 41 and 42, with automatic quality pause
  after both are durable;
- manual pause remains authoritative, so the release manifest alone cannot
  start work.

## 1. Decision

Three content-side Hermes roles are sufficient:

1. Primary Hermes: user conversation, intake clarification, and explicit user
   decisions. It does not own workflow state or silently rewrite an accepted
   brief.
2. Content Showrunner: one stateless authoring role for series strategy,
   single-video form/treatment/script, bounded copy repair, and production
   direction after copy approval.
3. Independent Content Critic: copy and production-plan review only. It cannot
   author or rewrite.

Do not add a fourth creative authority. If queue latency later becomes a real
bottleneck, horizontally replicate the same Showrunner or Critic service behind
the same logical endpoint and contracts.

Browser slots are not Hermes roles. They are execution fallbacks. API-backed
work keeps every slot dormant; a project wakes one sticky slot only for a real
browser fallback.

## 2. Evidence From The 50-Video Run

At the planning pause there are 37 locally composed videos. The project has
679 historical stage rows, including 249 visual-preview attempts and 209 visual
review attempts. Superseded and repair rows dominate the historical volume.

Observed successful-stage latency:

| Boundary | Count | Median | Maximum |
| --- | ---: | ---: | ---: |
| Legacy creative authoring | 66 | 81.1 s | 117.7 s |
| New Director checkpoint | 1 | 266.0 s | 266.0 s |
| Visual reference generation | 147 | 91.9 s | 262.9 s |
| Visual acceptance | 54 | 67.9 s | 94.9 s |
| Local final-asset preparation | 16 | 12.4 s | 27.5 s |
| Segment prompt preparation | 16 | 44.3 s | 53.5 s |

The newer isolated Director author requests average about 31 seconds and
independent Critic requests about 42 seconds. The 64 provider video tasks still
present in project state have a median database lifetime of about 381 seconds,
P95 of about 1,242 seconds, and maximum of about 3,866 seconds. Therefore the
media provider and visual repair loop, not the number of creative roles, are
the current throughput limits.

## 3. Creative Authority

Creative content must never come from a mother template. The Showrunner owns,
within the user brief and truth boundary:

- content form and premise;
- story or information architecture;
- opening mechanism and progression;
- audio mode, including no dialogue;
- complete copy and speaker identity when copy exists;
- conversion architecture only when the objective includes conversion;
- visual and sound grammar;
- cross-video diversity.

The platform may hard-code invariants, not creative answers. Valid invariants
include tenant isolation, product truth, provider limits, exact script-line
coverage, duration math, idempotency, reference ordering, local-file
completion, and retry bounds. Hooks, pain, story, presenter, product reveal,
CTA position, character count, shot style, and scene sequence are not global
invariants.

## 4. Universal Capability Graph

Each project is compiled into a capability graph instead of selecting a fixed
workflow template:

```text
intake + assets
  -> truth.normalize
  -> series.slate (only when count > 1)
  -> copy.write or nonverbal.treatment
  -> independent content review
  -> visual.plan + audio.design + copy.delivery.plan
  -> independent production-plan review
  -> reference generation and visual acceptance where required
  -> deterministic provider segment compile
  -> asynchronous generation/download/composition
  -> one publish guide per completed video
```

Nodes may be omitted when they are irrelevant. A silent mood film does not
need spoken-copy nodes. A screen tutorial may not need character anchors. A
product demonstration may reveal the product immediately. A documentary,
animation, explainer, comparison, interview, UGC-style piece, or entertainment
video may each select a different graph without new backend code.

## 5. Immutable Artifacts

The durable authority chain is:

1. `ProjectBrief`: objective, audience, truth, constraints, assets, platform,
   duration, and optional conversion contract.
2. `SeriesSlate`: reserved strategic territory and differentiation across the
   requested count.
3. `DirectedContentArtifact`: selected form, complete script or non-verbal
   treatment, audio mode, and segment allocation.
4. `DirectedProductionPlan`: visual beats, audio program, copy-delivery intent,
   reference needs, and continuity state.
5. `CompiledSegmentPacket`: exact provider duration, local timeline, reference
   bindings, immutable copy lines, and provider technical constraints.

Only the Showrunner authors items 2 through 4. The Critic approves exact hashes.
Runtime code signs, stores, schedules, compiles, and verifies them.

## 6. Remove The Dual Creative Path

The removed `CREATIVE` authoring stage must never be a start or restart target.
Historical rows remain readable only for already-produced assets.

The two remaining legacy-named stages must lose all creative authority:

- `CREATIVE_REVIEW` becomes `VISUAL_ACCEPTANCE`: pixel-grounded comparison of
  generated references with the signed production plan. It cannot review or
  rewrite story or copy.
- `VIDEO_PROMPTS` becomes `SEGMENT_COMPILE`: deterministic compilation from the
  signed production plan and provider capability registry. It cannot invent
  dialogue, pacing, camera choices, conversion, or references.

Use a compatibility read mapping for historical database rows during a bounded
migration. Do not keep two runnable implementations.

## 7. Parallel Pipeline

Parallelize independent resource lanes, not creative authorities:

- One series slate per project.
- Up to two variants may be planned/reviewed ahead of media during the first
  rollout.
- Reference generation may run for a later accepted variant while an earlier
  variant is in video generation.
- Start with two provider variants in flight because that is the current
  proven limit. Raise to three only after a canary shows no provider
  throttling, memory regression, duplicate submission, or quality dilution.
- Download, local verification, composition, and guide generation run
  asynchronously and independently per variant.
- One project may own at most one sticky browser slot, and that slot remains
  dormant for API work.

Suggested bounded buffers:

```text
Showrunner/Critic ready queue: 2 accepted variants
Reference-generation queue:    2 variants
Video-provider window:         2 initially, 3 after canary
Download/composition:           independent per task with one poll owner
```

Serial execution remains a legal operational fallback for a provider or
project whose configured in-flight limit is one. It is not a creative mode,
does not select a story structure, and must not be used as a mother template.
The current 50-video project is explicitly configured for a two-variant
provider window.

## 8. Scaling Rule

Do not scale by intuition. Record queue wait separately from model latency.
Add a replica only when all of the following hold for a sustained window:

- Showrunner or Critic queue-wait P95 exceeds 120 seconds;
- provider capacity is available and is being starved by planning;
- the replica uses the same stateless role, schema, truth packet, and review
  thresholds;
- idempotency keys make concurrent retry safe.

The current evidence does not meet this threshold.

## 9. Rollout Gates

Keep production paused until all gates pass:

1. No active or restart default references the removed `CREATIVE` stage.
2. Segment compilation is proven lossless for spoken, display-only, silent,
   music-only, and sound-design artifacts.
3. Heterogeneous zero-media benchmarks pass for product conversion,
   non-product tutorial, documentary/story, comparison/demo, and silent visual
   content.
4. API-only execution opens no browser or SSH tunnel.
5. Two-variant canary proves isolation, idempotency, local-file completion, and
   one guide per completed video.
6. Only then resume the remaining target-count work.
