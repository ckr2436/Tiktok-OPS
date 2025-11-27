# GMV Max backend architecture map

## 1. Overview

The backend supports TikTok Business **GMV Max** across binding discovery, campaign lifecycle management, metrics ingestion, creative heating, and balance checks. FastAPI routers expose provider/tenant scoped endpoints that enqueue Celery jobs for slow TikTok API calls, while synchronous reads serve cached campaign and metrics data persisted in the database. Core surfaces include campaign list/detail, metrics queries (daily/hourly and creative level), strategy preview/update, and creative heating triggers.

Processing follows a layered path: **FastAPI router → Pydantic schemas → service layer → TikTok GMV Max client → database repositories/models and Celery tasks**. Routers validate scope (workspace/provider/auth/store) and either call services directly or dispatch Celery tasks. Services orchestrate upserts into GMV Max tables, apply action logs/strategy configs, and invoke the typed TikTok client wrapper that normalizes requests to official GMV Max endpoints.

## 2. Module list by layer

### Routers

| File | Router prefix | Main endpoints (paths + methods) | Description |
| --- | --- | --- | --- |
| `backend/app/features/tenants/ttb/gmv_max/router_provider.py` | `/gmvmax` | Sync interval `GET/PUT /sync-interval`; sync kickoff and status `POST /sync`, `GET /sync/{task_id}`, `GET /tasks/{task_id}`; binding discovery `GET /binding_status`, `POST /binding/auto`, `POST /rebind_auto`; precheck `POST /precheck`; advertiser balance sync `POST /balance/sync`; campaign CRUD `POST /`, `PUT /{campaign_id}`, `GET /` and `GET /{campaign_id}`; metrics `POST /{campaign_id}/metrics/sync`, `GET /{campaign_id}/metrics`; campaign/creative actions `POST /{campaign_id}/actions`; action logs `GET /{campaign_id}/actions/logs`; creative heating list `GET /{campaign_id}/creative-heating`; strategy view/update/preview `GET /{campaign_id}/strategy`, `PUT /{campaign_id}/strategy`, `POST /{campaign_id}/strategies/preview`. | Provider-scoped GMV Max API surface used by the front-end; wires dependencies (binding resolution, TikTok client) and Celery task dispatch. |
| `backend/app/features/tenants/ttb/gmv_max/router_tenant.py` | `/tenants/{workspace_id}/gmvmax` | `POST /tenants/{workspace_id}/gmvmax/sync`, `GET /tenants/{workspace_id}/gmvmax/tasks/{task_id}`, `GET /tenants/{workspace_id}/tasks/{task_id}` | Tenant-scoped wrappers around the same Celery sync/status tasks for non-provider-specific entry points. |

### Schemas

| File | Schema name | Used by endpoint / service | Type |
| --- | --- | --- | --- |
| `backend/app/features/tenants/ttb/gmv_max/schemas.py` | `SyncRequest`, `SyncTaskResponse`, `SyncTaskStateResponse` | Sync kickoff/status routes, Celery callbacks | Request/response |
|  | `SyncIntervalUpdateRequest`, `SyncIntervalResponse` | Sync interval GET/PUT | Request/response |
|  | `CreateCampaignRequest`, `UpdateCampaignRequest`, `CampaignDetailResponse`, `CampaignListResponse`, `CampaignFilter`, `CampaignListOptions` | Campaign CRUD/list/detail routes and services | Request/response |
|  | `MetricsRequest`, `ReportRequest`, `MetricsResponse`, `ReportFiltering` | Metrics sync and query endpoints plus Celery report task | Request/response |
|  | `BalanceSyncRequest`, `GMVMaxPrecheckRequest/Response` | Balance sync and precheck routes/tasks | Request/response |
|  | `CampaignActionRequest/Response`, `ActionLogEntry` | Campaign action route and action-log listing | Request/response |
|  | `StrategyPreviewRequest/Response`, `StrategyUpdateRequest/Response`, `StrategyResponse` | Strategy view/update/preview endpoints | Request/response |
|  | `CreativeHeatingActionRequest/Response`, `CreativeHeatingRecord`, `CreativeHeatingListResponse` | Creative heating trigger/list endpoints and service layer | Request/response |
|  | `BindingStatusResponse`, `AutoBindingRequest/Response`, `AutoBindingCandidate` | Binding discovery routes | Response/internal |

### Service / domain logic

| File | Function / class | Responsibility |
| --- | --- | --- |
| `backend/app/features/tenants/ttb/gmv_max/service.py` | `sync_campaigns`, `list_campaigns`, `get_campaign` | Validate workspace/auth scopes then drive GMV Max campaign sync/list/detail using `ttb_gmvmax` service and TikTok client factory. |
|  | `sync_metrics`, `query_metrics` | Orchestrate hourly/daily metric sync and querying of stored metrics for a campaign. |
|  | `apply_campaign_action`, `list_action_logs` | Apply TikTok campaign actions and persist action logs; fetch paginated action logs. |
|  | `get_strategy`, `update_strategy`, `preview_strategy` | Manage local strategy config and compute previews based on recent metrics. |
| `backend/app/services/ttb_gmvmax.py` | `sync_gmvmax_campaigns`, `upsert_campaign_from_api` | Transform TikTok campaign payloads into DB rows and snapshots; merge duplicates. |
|  | `sync_gmvmax_metrics_hourly/daily` | Pull GMV Max report data and upsert hourly/daily metrics tables. |
|  | `create_gmvmax_campaign`, `update_gmvmax_campaign` | Wrap client create/update calls and persist campaign metadata. |
|  | `log_campaign_action`, `apply_campaign_action` | Persist action logs and proxy TikTok campaign actions/status changes. |
| `backend/app/services/gmvmax_heating.py` | `run_creative_heating_cycle` and helpers | Evaluate creative heating configs using stored metrics and TikTok action/apply APIs to auto-stop underperforming creatives. |
| `backend/app/services/gmvmax_heating_actions.py` | `apply_boost_creative_action` | Execute TikTok `campaign/gmv_max/action/apply` boost creative action and store heating/action log context. |
| `backend/app/services/ttb_api.py` | `list_gmvmax_stores`, `get_gmvmax_exclusive_auth`, `create_gmvmax_exclusive_auth`, `list_gmvmax_identities`, `recommend_gmvmax_bid`, `create/update/iter/get/report` helpers | Low-level TikTok API helpers used by the typed GMV Max client and service layer. |

### TikTok GMV Max client wrapper

`backend/app/providers/tiktok_business/gmvmax_client.py` defines `TikTokBusinessGMVMaxClient`, which wraps the official endpoints with typed requests/responses.

| Method | TikTok API (HTTP + path) | Request model | Response model |
| --- | --- | --- | --- |
| `gmv_max_campaign_get` | `GET /gmv_max/campaign/get/` | `GMVMaxCampaignGetRequest` | `GMVMaxCampaignListData` |
| `gmv_max_campaign_info` | `GET /campaign/gmv_max/info/` | `GMVMaxCampaignInfoRequest` | `GMVMaxCampaignInfoData` |
| `gmv_max_campaign_create` | `POST /campaign/gmv_max/create/` | `GMVMaxCampaignCreateRequest` | `GMVMaxCampaignInfoData` |
| `gmv_max_campaign_update` | `POST /campaign/gmv_max/update/` | `GMVMaxCampaignUpdateRequest` | `GMVMaxCampaignInfoData` |
| `campaign_status_update` | `POST /campaign/status/update/` | `CampaignStatusUpdateRequest` | `CampaignStatusUpdateData` |
| `gmv_max_campaign_action_apply` | `POST /campaign/gmv_max/action/apply/` | `GMVMaxCampaignActionApplyRequest` | `GMVMaxCampaignActionApplyData` |
| `gmv_max_session_create/update/list` | `POST /campaign/gmv_max/session/create/`, `POST /campaign/gmv_max/session/update/`, `GET /campaign/gmv_max/session/list/` | `GMVMaxSessionCreateRequest` / `GMVMaxSessionUpdateRequest` / `GMVMaxSessionListRequest` | `GMVMaxSessionListData` |
| `gmv_max_identity_get` | `GET /gmv_max/identity/get/` | `GMVMaxIdentityGetRequest` | `GMVMaxIdentityListData` |
| `gmv_max_store_list` | `GET /gmv_max/store/list/` | `GMVMaxStoreListRequest` | `GMVMaxStoreListData` |
| `gmv_max_store_shop_ad_usage_check` | `GET /gmv_max/store/shop_ad_usage_check/` | `GMVMaxStoreAdUsageCheckRequest` | `GMVMaxStoreAdUsageCheckData` |
| `gmv_max_occupied_custom_shop_ads_list` | `GET /gmv_max/occupied_custom_shop_ads/list/` | `GMVMaxOccupiedCustomShopAdsListRequest` | `GMVMaxOccupiedListData` |
| `gmv_max_video_get` / `gmv_max_custom_anchor_video_list_get` | `GET /gmv_max/video/get/`, `GET /gmv_max/custom_anchor_video_list/get/` | `GMVMaxVideoGetRequest` / `GMVMaxCustomAnchorVideoListGetRequest` | `GMVMaxVideoListData` / `GMVMaxCustomAnchorVideoListData` |
| `gmv_max_exclusive_authorization_get/create` | `GET /gmv_max/exclusive_authorization/get/`, `POST /gmv_max/exclusive_authorization/create/` | `GMVMaxExclusiveAuthorizationGetRequest` / `GMVMaxExclusiveAuthorizationCreateRequest` | `GMVMaxExclusiveAuthorizationData` |
| `gmv_max_bid_recommend` | `GET /gmv_max/bid/recommend/` | `GMVMaxBidRecommendRequest` | `GMVMaxBidRecommendation` |
| `gmv_max_report_get` | `GET /gmv_max/report/get/` | `GMVMaxReportGetRequest` | `GMVMaxReportData` |

### Database models

| Model (table) | Main fields | Purpose |
| --- | --- | --- |
| `TTBGmvMaxCampaign` (`ttb_gmvmax_campaigns`) | workspace/auth/advertiser/store IDs, campaign_id/name/status, roas_bid, budget, timestamps, raw_json, soft-delete flags | Cached GMV Max campaign metadata. |
| `TTBGmvMaxCampaignProduct` (`ttb_gmvmax_campaign_products`) | campaign/store/item_group ids, operation_status | Mapping of campaigns to promoted products. |
| `TTBGmvMaxCampaignSyncSnapshot` (`ttb_gmvmax_campaign_sync_snapshots`) | advertiser/store/campaign scope, raw_json, synced_at | Stores last synced payload for reconciliation. |
| `TTBGmvMaxMetricsHourly` (`ttb_gmvmax_metrics_hourly`) | campaign FK, store_id, interval_start/end, impressions/clicks/cost/orders/gross_revenue/roi/... | Hourly GMV Max metrics upserts. |
| `TTBGmvMaxMetricsDaily` (`ttb_gmvmax_metrics_daily`) | campaign FK, store_id, date, same metric fields as hourly | Daily GMV Max metrics upserts. |
| `TTBGmvMaxCreativeMetric` (`ttb_gmvmax_creative_metrics_daily`) | workspace/provider/auth IDs, campaign/creative ids, stat_time_day, metrics + raw snapshot | Creative-level daily metrics used for heating seed detection. |
| `TTBGmvMaxCreativeHeating` (`ttb_gmvmax_creative_heating`) | workspace/provider/auth IDs, campaign/creative ids, budget targets, evaluation thresholds, status fields | Stores creative heating configs and evaluation results. |
| `TTBGmvMaxActionLog` (`ttb_gmvmax_action_logs`) | campaign FK, action type/result, before/after payloads, actor label, error messages | Audit log for campaign actions and creative heating operations. |
| `TTBGmvMaxStrategyConfig` (`ttb_gmvmax_strategy_configs`) | workspace/provider/auth IDs, campaign FK, ROI/budget thresholds, cooldowns | Local strategy tuning for automated adjustments. |

### Celery tasks & beat schedules

| Task name | File | Purpose | Schedule / trigger |
| --- | --- | --- | --- |
| `ttb.sync_gmvmax` | `backend/app/tasks/ttb_gmvmax_tasks.py` | Beat-driven global sweep over GMV Max bindings to sync campaigns/metrics. | Periodic; guarded by Redis lock (used by beat). |
| `gmvmax.sync_campaigns` | same | Sync campaigns for a workspace/auth/advertiser scope into DB. | Scheduled every ~10 minutes via `scheduler_catalog` plus manual. |
| `gmvmax.sync_advertiser_balance` | same | Sync advertiser balance for GMV Max eligibility. | Manual; can be scheduled. |
| `gmvmax.sync_metrics` | same | Sync hourly or daily metrics for a campaign. | Hourly/daily schedules in `scheduler_catalog` (every 10 minutes for recent hours, daily at 03:00 UTC). |
| `gmvmax.apply_action` | same | Apply campaign action (start/pause/delete/update budget/strategy) and log. | Manual/strategy-driven; in beat catalog as manual. |
| `gmvmax.creative_heating_cycle` | same | Evaluate creative heating configs and stop creatives when thresholds missed. | Every 15 minutes via `scheduler_catalog`. |
| `gmvmax.precheck` | same | Run store/identity eligibility prechecks (usage, identity list, occupancy). | Triggered by `/precheck` route. |
| `gmvmax.report_get` | same | Fetch GMV Max report data asynchronously (metrics sync). | Enqueued by metrics sync route. |
| `gmvmax.strategy_preview` | same | Call bid recommendation API for strategy preview. | Enqueued by strategy preview route. |

## 3. Official TikTok API → backend mapping

| TikTok API | Client method | Service function(s) | FastAPI endpoint(s) | Celery tasks | DB tables touched |
| --- | --- | --- | --- | --- | --- |
| `GET /gmv_max/campaign/get/` | `gmv_max_campaign_get` | `sync_gmvmax_campaigns`, `list_campaigns` (via repositories) | `POST /gmvmax/sync`, `POST /tenants/{workspace_id}/gmvmax/sync` (async sync); implicit during sync/auto-bind flows | `gmvmax.sync_campaigns`, `ttb.sync_gmvmax` | `TTBGmvMaxCampaign`, `TTBGmvMaxCampaignProduct`, `TTBGmvMaxCampaignSyncSnapshot` |
| `GET /campaign/gmv_max/info/` | `gmv_max_campaign_info` | `get_campaign`, `create_gmvmax_campaign`, `update_gmvmax_campaign` (refresh) | `GET /gmvmax/{campaign_id}`; post-create/update responses | sync tasks and router refresh | `TTBGmvMaxCampaign`, `TTBGmvMaxCampaignProduct` |
| `POST /campaign/gmv_max/create/` | `gmv_max_campaign_create` | `create_gmvmax_campaign` | `POST /gmvmax` | direct from router | `TTBGmvMaxCampaign`, `TTBGmvMaxCampaignProduct` |
| `POST /campaign/gmv_max/update/` | `gmv_max_campaign_update` | `update_gmvmax_campaign` | `PUT /gmvmax/{campaign_id}`, `PUT /gmvmax/{campaign_id}/strategy` (campaign section) | router direct | `TTBGmvMaxCampaign`, `TTBGmvMaxCampaignProduct` |
| `POST /campaign/status/update/` | `campaign_status_update` | `apply_campaign_action` | `POST /gmvmax/{campaign_id}/actions` | `gmvmax.apply_action` | `TTBGmvMaxCampaign`, `TTBGmvMaxActionLog` |
| `POST /campaign/gmv_max/action/apply/` | `gmv_max_campaign_action_apply` | `apply_boost_creative_action`, heating flows | `POST /gmvmax/{campaign_id}/actions` (BOOST_CREATIVE) | heating cycle uses `gmvmax.creative_heating_cycle` | `TTBGmvMaxActionLog`, `TTBGmvMaxCreativeHeating` |
| `POST /campaign/gmv_max/session/create/update/` | `gmv_max_session_create`, `gmv_max_session_update` | `update_gmvmax_campaign`, strategy update flows | `PUT /gmvmax/{campaign_id}/strategy` | router direct | `TTBGmvMaxCampaign`, `TTBGmvMaxCampaignProduct` |
| `GET /campaign/gmv_max/session/list/` | `gmv_max_session_list` | `get_campaign` for sessions, strategy preview | `GET /gmvmax/{campaign_id}` | router direct | `TTBGmvMaxCampaign`, `TTBGmvMaxCampaignProduct` |
| `GET /gmv_max/report/get/` | `gmv_max_report_get` | `sync_gmvmax_metrics_hourly/daily`, creative heating metrics pull | `POST /gmvmax/{campaign_id}/metrics/sync` | `gmvmax.report_get`, `gmvmax.sync_metrics`, heating cycle | `TTBGmvMaxMetricsHourly`, `TTBGmvMaxMetricsDaily`, `TTBGmvMaxCreativeMetric` |
| `GET /gmv_max/bid/recommend/` | `gmv_max_bid_recommend` | Strategy preview, creative heating seed recommendation | `POST /gmvmax/{campaign_id}/strategies/preview` | `gmvmax.strategy_preview` | `TTBGmvMaxStrategyConfig` (context) |
| `GET /gmv_max/store/list/` | `gmv_max_store_list` | Auto-binding discovery | `POST /gmvmax/binding/auto`, `POST /gmvmax/rebind_auto` | `gmvmax.precheck` uses related checks | `TTBAdvertiserStoreLink` (via helper) |
| `GET /gmv_max/store/shop_ad_usage_check/` | `gmv_max_store_shop_ad_usage_check` | Binding discovery/precheck usage validation | `POST /gmvmax/binding/auto`, `POST /gmvmax/precheck` | `gmvmax.precheck` | `TTBAdvertiserStoreLink` (usage metadata) |
| `GET /gmv_max/identity/get/` | `gmv_max_identity_get` | Precheck identity listing | `POST /gmvmax/precheck` | `gmvmax.precheck` | none (response only) |
| `GET /gmv_max/occupied_custom_shop_ads/list/` | `gmv_max_occupied_custom_shop_ads_list` | Precheck occupancy | `POST /gmvmax/precheck` | `gmvmax.precheck` | none (response only) |
| `GET /gmv_max/exclusive_authorization/get/` / `POST /gmv_max/exclusive_authorization/create/` | `gmv_max_exclusive_authorization_get/create` | Binding discovery and persistence | `POST /gmvmax/binding/auto`, `POST /gmvmax/rebind_auto` | — | `TTBAdvertiserStoreLink` (binding hints), binding config tables |
| `GET /gmv_max/video/get/`, `GET /gmv_max/custom_anchor_video_list/get/` | `gmv_max_video_get`, `gmv_max_custom_anchor_video_list_get` | Campaign creation helpers | Used indirectly in creation flows if video anchors requested | — | `TTBGmvMaxCampaign` raw_json context |
| `GET /gmv_max/store/ad` usage check (shop_ad_usage_check) & `GET /gmv_max/store/list/` | `gmv_max_store_ad_usage_check`, `gmv_max_store_list` | Binding discovery | `POST /gmvmax/binding/auto` | `gmvmax.precheck` | `TTBAdvertiserStoreLink` |

## 4. Flow diagrams for key use cases

1. **GMV Max metrics sync (hourly/daily)**
   - Scheduler (see `scheduler_catalog.py`) or user triggers `POST /gmvmax/{campaign_id}/metrics/sync` → router builds `GMVMaxReportGetRequest` and enqueues `gmvmax.report_get`/`gmvmax.sync_metrics`.
   - Celery task (`gmvmax.sync_metrics`) opens DB session, loads campaign, and calls `sync_gmvmax_metrics_hourly` or `sync_gmvmax_metrics_daily` → TikTok client `gmv_max_report_get` → TikTok API.
   - Service parses report rows into `TTBGmvMaxMetricsHourly`/`TTBGmvMaxMetricsDaily` (and creative metrics when needed) with upserts.
   - Router/consumer polls `GET /gmvmax/tasks/{task_id}` to check status and then reads metrics via `GET /gmvmax/{campaign_id}/metrics` (which queries stored DTOs).

2. **GMV Max overview page data**
   - Frontend calls `GET /gmvmax` (campaign list) and `GET /gmvmax/{campaign_id}` (detail + sessions).
   - Router resolves binding/context → queries `TTBGmvMaxCampaign` rows and, for detail, concurrently fetches TikTok `campaign/gmv_max/info` and `campaign/gmv_max/session/list` via client methods.
   - Upserts refreshed campaign/session data via `upsert_campaign_from_api`, then returns `CampaignDetailResponse`/`CampaignListResponse` schemas.

3. **Campaign management & creative heating**
   - **Create/update**: `POST /gmvmax` or `PUT /gmvmax/{campaign_id}` → service `create_gmvmax_campaign`/`update_gmvmax_campaign` → client `gmv_max_campaign_create/update` → TikTok API; router refreshes detail via `gmv_max_campaign_info` and returns `CampaignDetailResponse`.
   - **Status/actions**: `POST /gmvmax/{campaign_id}/actions` with action type → router builds `CampaignStatusUpdateRequest` or `GMVMaxCampaignUpdateRequest` → client `campaign_status_update`/`gmv_max_campaign_update` → updates `TTBGmvMaxActionLog` + snapshot refresh.
   - **Creative heating**: same action endpoint with `BOOST_CREATIVE` or periodic `gmvmax.creative_heating_cycle` → service `apply_boost_creative_action` or `run_creative_heating_cycle` → client `gmv_max_campaign_action_apply` and `gmv_max_report_get` for metrics → persists `TTBGmvMaxCreativeHeating` + action logs and evaluation results.

## 5. How to debug GMV Max

- Identify the frontend call (e.g., campaign list, metrics sync) and map to the router path in `router_provider.py`; check the associated schema for required parameters.
- Trace router → service (`backend/app/features/tenants/ttb/gmv_max/service.py` and `backend/app/services/ttb_gmvmax.py`) to see which TikTok client method is invoked and which DB tables are written.
- Inspect Celery tasks in `backend/app/tasks/ttb_gmvmax_tasks.py` to confirm whether a background sync is still running or failing (task names logged, beat schedule in `scheduler_catalog.py`).
- Verify DB tables: campaigns (`ttb_gmvmax_campaigns`), metrics (`ttb_gmvmax_metrics_hourly/daily`, `ttb_gmvmax_creative_metrics_daily`), strategy (`ttb_gmvmax_strategy_configs`), and action logs/heating (`ttb_gmvmax_action_logs`, `ttb_gmvmax_creative_heating`).
- For TikTok API issues, check the client wrapper method corresponding to the API path to confirm payload normalization, then review router/service logging (`gmv.ttb.gmvmax.router`, `gmv.tenants.gmvmax`, `gmv.tasks.gmvmax`).
