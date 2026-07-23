# Hermes content critic

This is the physically isolated, API-only Hermes gateway for independent
content criticism.

Runtime:

- Home: `/home/hermes/.hermes-content-critic`
- Endpoint: `http://127.0.0.1:8646/v1`
- Model alias: `gmv-ops-hermes-content-critic`
- Service: `hermes-content-critic.service`

The backend client remains disabled until
`HERMES_CONTENT_CRITIC_AGENT_ENABLED` is explicitly enabled. Failure must fail
closed and must not route to the director, primary, or advertising Hermes
instances.

Deployment may copy only provider credential files from another isolated
Hermes home. Never copy sessions, response stores, memories, logs, caches,
cron state, or gateway state.

The deployed Hermes source must include the stateless Responses patch stored at
`ops/hermes-content-director/hermes-stateless-responses.patch`. Every request
uses `store=false`; both transcript JSON and SQLite session persistence must
remain empty during live verification.

Deploy with:

```bash
ops/hermes-content-director/install-isolated-content-role.sh critic
```

The installer allowlists only the API authentication key and selected provider
credential. It never copies role port/model values from another runtime.
