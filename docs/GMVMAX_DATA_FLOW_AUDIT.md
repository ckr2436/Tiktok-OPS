# GMV Max Data Flow Audit

## Source of truth

TikTok reporting is the external source of truth. MySQL is the serving and
history layer. A React Query refetch only reads MySQL; it is not a TikTok sync.
Every metrics response therefore includes a `freshness` object with source,
last sync age, advertiser timezone, range, row count, and quality state.

## Frontend requests

| UI workflow | API | TikTok call | Persistence / read path |
| --- | --- | --- | --- |
| Overview page load after binding | `POST /gmvmax/sync` plus account metadata, products and balance sync | GMV Max report by requested levels; account/store/product/balance APIs | Writes catalog, overview snapshots, campaign/hourly metrics, creative daily metrics; then invalidates queries |
| Overview auto refresh (10/15/20/30 minutes) | `GET /gmvmax/metrics` | No | Reads MySQL only |
| Overview manual Sync button | `POST /gmvmax/sync` | Yes | Runs account-scoped Celery sync and refetches only after terminal success |
| Detail page load/range/filter | `GET /gmvmax/{campaign}/metrics` | No | Campaign/product reads daily + hourly fact tables; creative reads historical daily plus latest current-day 10-minute snapshot |
| Detail manual Sync button | `POST /gmvmax/campaigns/{campaign}/metrics/sync` | Yes | One authoritative account-level sync for CAMPAIGN, PRODUCT and CREATIVE; no duplicate sync |
| Campaign pause/start/delete/budget | `POST /gmvmax/{campaign}/actions` | Yes | Calls campaign status/update API and writes action log/catalog state |
| Creative remove/add back | `POST /gmvmax/{campaign}/actions` | Yes | Calls GMV Max creative status update and writes guard/action history |
| Creative candidate refresh | `GET /gmvmax/creative-assets?refresh=true` | Yes when refresh requested | Updates asset cache; ordinary metrics reads remain DB-only |

## Background synchronization

| Job | Effective cadence | TikTok API | Main writes |
| --- | --- | --- | --- |
| `ttb.sync_gmvmax` | Configured 10/15/20/30 minutes | GMV Max campaign get/info | `gmvmax_product_campaign_catalog`, campaign-product bindings |
| `gmvmax.sync.run_scheduler` | Scheduler checks every minute | GMV Max report get | Overview/campaign/product/creative fact tables according to monitoring strategies |
| `gmvmax.sync_creative_metrics_10min` | Sweep every minute; prioritizes least-fresh active campaigns | GMV Max report get, CREATIVE level | `gmv_creative_metrics_10min`, asset cache |
| `gmvmax.smart_guard_cycle` | Beat every minute; each strategy dynamically due every 1-5 minutes | GMV Max report get, CAMPAIGN level | `gmv_campaign_realtime_state`, campaign daily fact, guard events, valid Hermes samples |
| `gmvmax.creative_guard_cycle` | Beat every minute; each strategy dynamically due every 1-5 minutes | Actions only after data gate passes | Reads current-day 10-minute/daily facts; writes exclusions, resets and guard events |
| `gmvmax.hermes_advisor_cycle` | 10 minutes | LLM only when review is needed | Recommendations and strategy config |
| `gmvmax.hermes_daily_report` | 13:30 UTC, previous advertiser-local day | LLM | Daily report, recommendation and policy memory tables |

## Data tables

| Purpose | Tables |
| --- | --- |
| Campaign metadata/status | `gmvmax_product_campaign_catalog`, `gmvmax_product_campaign_item_groups` |
| Exact overview range | `gmv_overview_snapshots` (`MANUAL`) |
| Campaign facts | `gmvmax_product_campaign_metrics_daily`, `gmvmax_product_campaign_metrics_hourly` |
| Creative facts | `gmvmax_product_creative_metrics_daily`, `gmv_creative_metrics_10min` |
| Realtime guard state | `gmv_campaign_realtime_state`, `gmv_campaign_guard_events` |
| Automation configuration | `gmv_strategy_configs` |
| Hermes learning/reporting | `gmv_hermes_ad_learning_samples`, `gmv_hermes_ad_recommendations`, `gmv_hermes_ad_daily_reports` |
| Shop order timing | `ttb_shop_order_import_batches`, `ttb_shop_order_facts` |

## Decision quality gates

1. The smart guard directly requests the advertiser-local current day on every
   due cycle. Empty reports are `HOLD`, never zero-performance decisions.
2. Same-day cost, GMV, or order counter regression is held until the same
   corrected cumulative snapshot is observed twice.
3. Creative actions require current advertiser-day campaign and creative data
   no older than the configured maximum (10 minutes by default).
4. A rejected quality sample is recorded for audit but is not written into
   Hermes learning data.
5. Manual sync is atomic for report windows. Upstream partial failures surface
   as task failures instead of a false green success.
6. Current-day campaign daily/hourly rows are selected by actual ingestion age,
   with source priority used only as a tie-breaker.
7. Current-day creative display and creative guard both use the latest coherent
   snapshot for each creative/day, not independent maxima from different pulls.
8. Campaign, product and creative momentum are assembled into one decision
   snapshot. Cross-source order/GMV conflicts trigger a short protective pause,
   a forced CAMPAIGN/PRODUCT/CREATIVE refresh and an attribution hold.
9. A cumulative creative snapshot without a prior window baseline is never
   counted as recent incremental spend or orders.
10. Smart Guard applies an immediate 15-minute protection pause before waiting
    for an LLM. Hermes then reviews long cooldowns, recovery and profitable
    budget expansion with bounded APPROVE/REVISE/HOLD output.
11. A recovery timestamp is an earliest review time, not a guaranteed start.
    Recovery still requires a fresh consistent snapshot and Hermes approval.

## Freshness states

- `fresh`: current-day data is within its allowed latency.
- `stale`: rows exist but are too old for decisions.
- `missing`: no persisted report exists for the requested scope/range.
- `historical`: the requested range ends before the advertiser-local current day.

Only `fresh` current-day observations may trigger automated status, budget or
creative actions. Historical data remains available for analysis and Hermes
reports but cannot masquerade as realtime data.
