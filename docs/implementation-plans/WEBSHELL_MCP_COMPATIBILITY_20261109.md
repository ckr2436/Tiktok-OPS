# Durable native WebShell transport hooks — issue7

This is the companion native-router change for [server-ops-control PR6](https://github.com/ckr2436/server-ops-control/pull/6) / [issue7](https://github.com/ckr2436/server-ops-control/issues/7). Scope is MCP/WebShell channel only; no A17 source, optimization, tests or deployment.

The same deployed normal WebShell/SavedCredentialEnvelope/original RootSu/PAM owns every kernel PTY. Optional trusted Python-only session limits and delivery callbacks let PR6 supervise the original native ASGI session independently of short external WebSocket RPCs. Browser clients cannot select those hooks or change limits/identity through init fields. Native browser defaults and the root saved-credential flow remain unchanged. This adds no root Broker, password store, forkpty implementation or tmux daemon.

Durable service raw output forwards background gmv bytes immediately rather than retaining/truncating the browser's 256KiB buffer. Explicit native delivery acknowledgement means written_to_pty, not per-command completion. Original root control frames expose actual su/controller PIDs and verified UID for recovery checks. Native login/eligibility and saved credential-version revocation remain authoritative through PR6's injected loader/watchdog. Default browser throttling/lifetimes remain unchanged; service limits come from an explicit durable owner enrollment. PAM rate limits are preserved.

PR6 persists encrypted enrollment, registry, exact byte checkpoints and idempotency receipts, and provides an independent noninteractive native-root job runner with real stdout/stderr/exit receipts. MCP stays non-root/NoNewPrivileges. Existing Tunnel and SO_PEERCRED transport are reused. Full-host-root execution remains destructive/open-world, plaintext and subject to platform safety checks, without a custom command whitelist.

Real ordinary ChatGPT has completed three independent turns in the same host-root PTY, retaining UID0/root/slkSrv, /tmp, exported variable and identical Shell PID across a forced MCP/adapter/original Tunnel restart WHILE its noninteractive job ran. It then read that original job's actual exit7 and separate stdout/stderr; no command resubmission. PR6 contains real fault receipts and byte-perfect1MiB local SDK pagination, actual timeout/cancel process results, negative auth tests and114 isolated MCP/channel tests. Final logout/credential revocation acceptance and owner handoff are tracked there; keep Draft pending those final results.

Controller/host crash is explicitly interrupted_no_replay and cannot restore lost kernel PTYs; no fake resume/replay. Private single owner, no multi-user OAuth; enrolled lifetime is capped by native login expiry. Resource quotas/idle/absolute limits and tampering by an already-authorized host root remain documented constraints.

Live originals/dirty checkout are preserved by an immutable, reversible router overlay. webshell_su.py remains the exact existing production source imported in the earlier PR stage. No merge/deployment of unrelated source is authorized.

---
## Historical finite-root stage (superseded by durable PR6)

# Existing production WebShell compatibility

This updates Draft PR417 to match slkSrv's actual deployed, dirty WebShell source (SHA256 a4942f9dd8005a20ed6e065ce96b66306b701b8864a345bb75177046cce1bb05) before applying optional service-auth/raw-output hooks. The root-switch logic and webshell_su.py are imported verbatim from the existing production implementation, rather than newly developed here. No production checkout/release file is overwritten.

The deployed normal_shell.SavedCredentialEnvelope and credentials.load_password already perform user/workspace-scoped saved-credential lookup and existing RootSu/PAM verification. Their live hashes and the reversible router overlay are pinned in server-ops-control PR6's ops/mcp-webshell/production-manifest.json and production/upstream/live-compatibility.patch. This PR does not introduce another root Broker, credential store or PTY design.

Only the router's optional admin-loader/spawner/raw-output hooks, explicit mode identity metadata, and bounded output-tail drain are new adaptations. Browser defaults retain cookie/Origin/RBAC and current normal-wrapper v2 login revocation. Production S2S delegation uses that same original WebShell process and session registry via a private Unix listener; its implementation, deployment/rollback, schemas and audit evidence are in https://github.com/ckr2436/server-ops-control/pull/6 . Keep both PRs Draft; root ChatGPT acceptance must be reported from actual correlated receipts, not inferred from local tests. A17 is outside this change.
