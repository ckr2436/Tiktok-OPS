# GMV Max metrics 404 troubleshooting

When calling the provider metrics endpoint at
`/api/v1/tenants/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmvmax/metrics`,
a `404` response with the message `campaign not found in cache; trigger refresh first`
comes from the campaign-detail handler (`/{campaign_id}`). That handler returns 404
whenever the requested campaign is missing from the local cache.

FastAPI routes under `/gmvmax` include a static `/metrics` path and a parameterized
`/{campaign_id}` path. If there are no cached campaigns for the account (for example,
before running a GMV Max sync), the path matcher resolves `metrics` to the
`/{campaign_id}` route and returns the cache-miss error. Sync the account first
(via the `/gmvmax/sync` endpoints) to populate campaigns, then re-run the metrics
query.
