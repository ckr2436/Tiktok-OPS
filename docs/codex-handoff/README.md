# Codex Handoff: GMV OPS

Updated: 2026-07-18 Asia/Shanghai

## Purpose

This directory transfers the durable engineering context from the long-running
GMV OPS collaboration to Codex running directly on the production server. It
is an operational specification, not a transcript and not a replacement for
the current code, database, tests, or logs.

No secrets are included. Never add passwords, API keys, cookies, OAuth tokens,
ChatGPT session data, or hidden prompts to this directory.

## Product Summary

GMV OPS is a multi-company, multi-user operations platform. Its major active
areas are:

- TikTok Business authorization and GMV Max management.
- Hermes-assisted advertising decisions and reports.
- Company product library.
- Hermes Content Factory for creative planning, visual references, AI video
  generation, local video storage, and per-video editing guidance.
- AI provider key management and model-based routing.
- User/device-owned Windows browser bridge for ChatGPT fallback.
- Website Ads and supporting media, reporting, and tracking workflows.

The current collaboration concentrated on Content Factory and AI video, while
`../CODEX_SERVER_HANDOFF.md` records a separate GMV Max handoff.

## Document Map

- `01_SYSTEM_BOUNDARIES.md`: product ownership, data isolation, security, and
  module boundaries.
- `02_CONTENT_FACTORY.md`: the production workflow, state machine, asset
  provenance, variant strategy, video prompt contract, and recovery semantics.
- `03_PROVIDERS_AND_BROWSER_BRIDGE.md`: API provider registry, model
  capabilities, browser devices, slots, uploads, popup/rate-limit handling, and
  idempotency.
- `04_OPERATIONS_AND_TESTING.md`: runtime layout, services, test commands,
  deployment discipline, health checks, and incident workflow.
- `05_CURRENT_STATE_AND_REGRESSIONS.md`: current verified state, major fixes,
  anti-regression checklist, and known residual risks.
- `06_CONTENT_FACTORY_OPTIMIZATION.md`: production-run evidence, non-hardcoded
  target architecture, queue/memory isolation, event-driven execution, and
  staged migration plan.
- `handoff_manifest.json`: machine-readable paths, services, stages, providers,
  and test suites.

## How The Next Codex Should Begin

1. Work from `/opt/gmv/GMV-OPS`.
2. Read root `AGENTS.md` and every file in this directory.
3. Run `git status --short`. The dirty tree is expected and valuable.
4. Inspect current service state and recent journals.
5. Read the source and tests named for the requested subsystem.
6. Query live state using read-only scripts before changing production.
7. Fix source code, add tests, deploy, and verify. Do not treat a database row
   edit or manual task requeue as a complete fix.

## Source Of Truth Order

When information conflicts, use this order:

1. Explicit newest user instruction.
2. Current production code and database schema.
3. Current tests and service configuration.
4. Official provider documentation stored or linked by the project.
5. This handoff.
6. Old conversation assumptions and legacy comments.
