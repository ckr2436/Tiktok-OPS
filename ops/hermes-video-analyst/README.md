# Hermes video analyst

This is the stateless, API-only visual analyst for TikTok Shop and GMV Max
content operations. It calls the local GMV AI Gateway logical model instead of
binding directly to one vendor. The gateway applies operator priorities,
health checks, circuit breaking and provider failover. The analyst accepts only
low-detail bounded visual evidence, has no tools or memory, and never shares the
Content Factory runtime.

- Home: `/home/hermes/.hermes-video-analyst`
- Endpoint: `http://127.0.0.1:8647/v1`
- Model alias: `gmv-ops-hermes-video-analyst`
- Logical role: `gmv-shop-video-analyst-v1`
- Provider policy: Sub2API `gpt-5.6-luna` first, then the verified
  `video-analyst-gpt-5.4-mini` provider pool.
- Gateway: `http://127.0.0.1:8650/v1`
- Service: `hermes-video-analyst.service`
- Queue: `gmv.tasks.video_analysis`

Install the gateway with `install-isolated-video-analyst.sh`, then install the
API and queue-worker drop-ins from `ops/systemd`. The worker uses concurrency
one, prefetch one, bounded task recycling, ffmpeg timeouts, and a systemd memory
ceiling. Normalized contact sheets live only in a per-task temporary directory
and are removed in `finally`; neither contact sheets nor Base64 are persisted.

`hermes-stateless-no-request-dump.patch` also prevents a failed `store=false`
request from writing its visual payload into Hermes error-debug files.

The installer also verifies `hermes-api-server-max-tokens.patch`. Upstream
Hermes otherwise ignores `model.max_tokens` on its API-server path and asks the
provider for its much larger model default. Keeping the patch applied caps both
cost exposure and response memory for this role (and honors existing role caps).
