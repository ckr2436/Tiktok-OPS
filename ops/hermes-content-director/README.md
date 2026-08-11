# Hermes content director

This is the isolated, API-only Hermes gateway for the universal content
Showrunner. One physical role performs stateless whole-series strategy,
single-video writing/revision, and production direction through separate
structured request modes. It intentionally has no tools, browser, memory,
skills, fallback provider, or stored conversation dependency.

The Showrunner chooses creative form from the current project's objective,
audience, truth, policies, and registered production capabilities. It does not
use a content mother template. Deterministic workflow state, exact delivery
timing, provider segmentation, asset routing, retries, and downloads remain
backend responsibilities.

Runtime:

- Home: `/home/hermes/.hermes-content-director`
- Endpoint: `http://127.0.0.1:8645/v1`
- Model alias: `gmv-ops-hermes-content-director`
- Service: `hermes-content-director.service`

The backend client remains disabled until
`HERMES_CONTENT_DIRECTOR_AGENT_ENABLED` is explicitly enabled. Endpoint failure
must fail closed and must not route to the primary, critic, or advertising
Hermes instances. Independent copy criticism runs through the separate
`hermes-content-critic.service` on port 8646.

Only the provider credential files are copied from an existing isolated Hermes
home during deployment. Never copy sessions, response stores, memories, logs,
caches, cron state, or gateway state into this home.

The deployed Hermes source must include `hermes-stateless-responses.patch`.
Upstream `store=false` originally disabled only Responses API retrieval while
the underlying agent still wrote transcript JSON and SQLite session rows. The
patch carries the flag through both streaming and non-streaming Responses paths
and disables both persistence sinks. Reapply and rerun its focused upstream
tests after every Hermes upgrade.

Deploy or refresh either isolated role with:

```bash
ops/hermes-content-director/install-isolated-content-role.sh director
ops/hermes-content-director/install-isolated-content-role.sh critic
```

After both gateways are healthy, install the backend role-enablement drop-ins
and restart only the API and content-control worker:

```bash
install -m 0644 ops/systemd/gmv-api-hermes-content-roles.conf \
  /etc/systemd/system/gmv-api.service.d/hermes-content-roles.conf
install -m 0644 ops/systemd/gmv-celery-hermes-content-roles.conf \
  /etc/systemd/system/gmv-celery-worker@gmv.tasks.hermes_agent.service.d/hermes-content-roles.conf
systemctl daemon-reload
systemctl restart gmv-api.service \
  gmv-celery-worker@gmv.tasks.hermes_agent.service
```

The dedicated enable flags are independent from `HERMES_AGENT_ENABLED`.
Disabling the primary Hermes must not disable the stateless Director or
Critic, and enabling either content role must never enable advertising roles.

The installer copies only the API authentication key and selected provider
credential. It writes the role-specific port and model itself, so a copied
Director `.env` cannot accidentally make the Critic bind to port 8645.
