# Hermes content producer

This is the isolated, API-only conversational intake role for Content Factory.
It uses a dedicated Hermes home, response store and port so its session chain
cannot overlap Director, Critic, browser slots, advertising agents or another
Producer deployment.

- Home: `/home/hermes/.hermes-content-producer`
- Endpoint: `http://127.0.0.1:8648/v1`
- Model alias: `gmv-ops-hermes-content-producer`
- Service: `hermes-content-producer.service`

Each backend request supplies both a stable scoped conversation name and the
latest authoritative database working brief. Hermes conversation continuity is
therefore useful for natural dialogue but never becomes the source of truth.
Built-in file memory remains disabled because it is process-global rather than
workspace/user scoped.

Deploy with:

```bash
ops/hermes-content-director/install-isolated-content-role.sh producer
```
