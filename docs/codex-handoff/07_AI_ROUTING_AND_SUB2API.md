# Unified AI Routing, Sub2API And Flow2API

Updated: 2026-07-25 Asia/Shanghai

## Runtime Boundary

All production Hermes text and multimodal roles call the local OpenAI-compatible
GMV gateway at `127.0.0.1:8650`. Hermes processes keep their separate homes,
ports, memory and tool boundaries; they do not own upstream provider keys.

The gateway selects normal `AiModelRoute` records and records metadata-only
`AiRouteAttempt` rows. Prompts, images, API keys and model response bodies must
not be persisted in routing audit data.

## Provider Policy

Within an equivalent model tier the intended order is:

1. Self-hosted Sub2API.
2. ToAPIs when healthy and funded.
3. Coultra disaster fallback.

OpenRouter is excluded from automatic production role policies. Circuit state
is authoritative, so an unhealthy higher-ranked provider is skipped until its
bounded recovery window expires.

Model tier and provider order are separate concepts. A role may prefer Terra
then Luna for quality, or Luna then Terra for latency. The platform role page
changes provider order inside every tier and persists the operator override on
materialized routes.

Image generation uses independent provider identities. Sub2API owns
`gpt-image-2`; Flow2API owns `nano_banana_pro` through provider model
`gemini-3.0-pro-image`. Content Factory visual routing prefers Sub2API GPT Image
at priority 1, then Flow2API Nano Banana Pro at priority 5, then the configured
aggregator fallbacks. A Flow failure never opens the Sub2API circuit, and a
Sub2API failure never suppresses the Flow account pool.

Flow2API image requests use `/v1/chat/completions`, ordered multimodal image
parts, and the body-level `gmv_idempotency_key`. The response is not successful
until its image URL has been downloaded, size-bounded, decoded and stored on
local RAID. The former `sub2api_gemini_images` route is retired and retained
only as disabled audit history.

## Logical Roles

Role policy is versioned at `ops/hermes-unified-routing/routing-policy.json`.
Active non-content aliases are:

- `gmv-hermes-general-v1`
- `gmv-hermes-general-aux-v1`
- `gmv-hermes-general-vision-v1`
- `gmv-ads-realtime-v1`
- `gmv-ads-review-v1`
- `gmv-shop-video-analyst-v1`

Content Director, Critic and Visual Inspector retain their existing logical
aliases and behavior. The same generic role materializer supports both policy
files without merging Hermes histories, tools or state.

## Gateway Contract

The chat gateway supports text, multimodal input, tools, `tool_choice`, parallel
tool calls, structured response formats, bounded role-specific retry envelopes
and OpenAI-compatible SSE output. Provider calls finish before SSE headers are
committed so another route can be selected safely.

Policy errors fail closed and do not hop providers. Quota, rate-limit, network
and 5xx failures use bounded circuit and failover behavior. Advertising model
output remains advisory and cannot bypass deterministic advertising guards.

## Configuration And Secrets

`backend/scripts/configure_hermes_unified_gateway.py` updates only the provider
surface of each Hermes config, preserves role isolation settings, creates a
timestamped backup, and removes obsolete upstream keys from Hermes homes.
Systemd supplies only `GMV_AI_GATEWAY_KEY` from `/etc/gmv/ai-gateway.env`.

`backend/scripts/sync_hermes_provider_keys.py` is retained as a compatibility
entrypoint for the existing path unit, but now synchronizes logical routes only.
It must never decrypt or distribute upstream credentials.

## Deployment And Rollback

Before restart, verify queues are empty and create backups of changed sources,
Hermes configs and the frontend. Restart the gateway before dependent Hermes
services, then restart only the API and video-analysis worker when their code
changed.

Verify tool calls, JSON output, multimodal input, Sub2API selection and one
forced fallback. Rollback is configuration-only: restore the timestamped
Hermes config backup and previous systemd drop-ins, then restart affected
services. Database routes can be disabled without deleting audit history.
