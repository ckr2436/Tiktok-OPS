# GMV Max Troubleshooting

## Unable to load store products (HTTP 400/422)

**Symptoms**

* GMV Max Overview page shows “Failed to load products” and the network panel reports an HTTP 400/422 when calling `/products`.
* A browser alert similar to “获取产品列表失败，原因是: 400” appears.

**What the API expects**

The tenant endpoint `/api/v1/tenants/{workspace_id}/providers/{provider}/accounts/{auth_id}/products` requires:

1. A `store_id` query parameter (FastAPI validates this at the router layer and responds with HTTP 422 if it is missing).【backend/app/features/tenants/ttb/router.py†L1439-L1452】
2. The selected store must exist under the same workspace/auth pair; otherwise `_get_store` raises `STORE_NOT_FOUND`.【backend/app/features/tenants/ttb/router.py†L1454-L1464】
3. If an advertiser is selected, the store must be linked to that advertiser, otherwise `ADVERTISER_STORE_LINK_NOT_FOUND` is thrown.【backend/app/features/tenants/ttb/router.py†L1486-L1503】
4. When a business center (`owner_bc_id`) is supplied, the store’s business center must match; otherwise the router raises `BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE`.【backend/app/features/tenants/ttb/router.py†L1466-L1485】

Because of these validations, incomplete scope selections in the UI (missing store, advertiser, or mismatched BC) will cause the backend to reject the request with the 4xx error you are seeing.

**How to resolve**

1. In GMV Max Overview, complete every scope filter: account → business center → advertiser → store. The UI only enables the product query after all four filters are populated, but refreshing the page with stale local storage can leave an invalid combination selected; cycling through the dropdowns forces a fresh selection that matches the backend data.
2. If the scope dropdowns are empty, run **Sync account metadata** so that business centers, advertisers, and stores are fetched again from TikTok. This ensures the linkage tables used by the backend are up to date before fetching products.
3. When switching to a different business center or advertiser, re-select the store so the trio stays aligned. The store list is filtered by the advertiser, but a cached selection from a previous advertiser can be persisted locally and trigger the link validation error described above.
4. If the error persists after refreshing scope data, verify in the admin panel that the advertiser ↔ store relationship exists and that both belong to the same business center.

Following the above sequence guarantees that the `/products` request satisfies the backend preconditions and the products list will load normally.

## Products list is empty (HTTP 200 with `items: []`)

**Symptoms**

* `/products` succeeds but returns `total: 0` even though TikTok shows many GMV-eligible SKUs.
* GMV Max Overview renders “No products available” in both the unassigned list and the create-series modal.

**What the API actually does**

* `list_account_products` never calls TikTok. It simply reads from the local `ttb_products` table using the selected `workspace_id`, `auth_id`, and `store_id`, and it will happily return an empty list if that table has zero rows for the current store.【F:backend/app/features/tenants/ttb/router.py†L1441-L1544】
* The only code path that fills `ttb_products` is the `scope=products` sync task. `TTBSyncService.sync_products` walks every advertiser↔store link, streams products from TikTok, and upserts them into the local table; without running that task the query has nothing to return.【F:backend/app/services/ttb_sync.py†L1182-L1269】
* Saving the GMV Max binding with `auto_sync_products=true` wires an interval schedule that repeatedly enqueues `ttb.sync.products`, so the table stays warm without manual clicks.【F:backend/app/services/ttb_binding_config.py†L115-L181】

**How to resolve**

1. On the TikTok Business binding page, click **同步 Product** (scope=`products`). That enqueues the `ttb.sync.products` job for the entire account, allowing the `/products` API to read real rows as soon as the job finishes.
2. If you want the GMV Max page to stay populated automatically, enable the “Auto sync products” toggle when saving the binding so the scheduler keeps running the same task in the background.
3. Repeat the manual sync any time you onboard a new store or advertiser link but are not yet ready to enable the automatic schedule.

## Why the UI fired two `/products` requests after syncing

**Symptoms**

* The browser network log shows two identical `GET /products` calls every time you click **同步 GMV Max** or finish creating/updating a series.

**Root cause**

* The GMV Max Overview page invalidated the React Query cache _and_ immediately called `productsQuery.refetch()` in `handleSync`, `handleSeriesCreated`, and `handleSeriesUpdated`, so each user action triggered two fetches in quick succession.【F:gmv-frontend/src/features/tenants/gmv_max/pages/GmvMaxOverviewPage.jsx†L2831-L2956】

**Current behavior**

* The page now uses a shared `refreshScopeQueries()` helper that invalidates both the campaigns and products queries exactly once and awaits the result. Syncing, creating, or editing a series issues a single `/products` request, eliminating the duplicated traffic in the screenshot you captured.【F:gmv-frontend/src/features/tenants/gmv_max/pages/GmvMaxOverviewPage.jsx†L2738-L2956】
