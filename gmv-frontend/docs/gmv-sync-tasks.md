# GMV Ops 同步任务梳理

下表列出了前端会触发异步后端任务并轮询状态的场景，覆盖 GMV Max 相关页面。

| 场景/页面 | 触发接口 | 状态查询 | 前端封装与调用 | 说明 |
| --- | --- | --- | --- | --- |
| GMV Max 总览页（系列/店铺列表、概览指标刷新） | `POST /tenants/{wid}/gmvmax/sync`（`startGmvMaxSync`，携带 provider、auth_id 与 scope 参数） | 后端返回的 `status_url`（形如 `/tenants/{wid}/gmvmax/tasks/{task_id}`） | `useGmvSyncTask` 在 `GmvMaxOverviewPage.jsx` 中创建任务并通过 `useBackendTaskPolling` 轮询；`useEnsureFreshGmvData` 复用同一套逻辑用于详情页预取 | 终态停止轮询并刷新 `composeMetricsQueryBaseKey(..., 'all')` 下的 React Query 缓存 |
| GMV Max Campaign 详情页（指标/创意/产品指标刷新） | `POST /tenants/{wid}/providers/{provider}/accounts/{authId}/gmvmax/{campaignId}/metrics/sync`（`syncGmvMaxMetrics`） | 同步返回的 `status_url`（同样指向 `/tenants/.../gmvmax/tasks/{task_id}`） | `useGmvMaxMetricsSync` 在 `GmvMaxCampaignDetailPage.jsx` 中触发，内部使用 `useBackendTaskPolling` 轮询任务终态并刷新 `composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId)` | 终态刷新当前 Campaign 的指标查询；失败时提示“GMV Max 数据同步失败，请稍后重试。” |

> 备注：`useEnsureFreshGmvData` 在详情页的手动“同步数据”流程中用于 10 分钟节流的“前置同步”，它通过 `POST /tenants/{wid}/gmvmax/sync` 先刷新本地 Campaign/Store 映射及快照（Celery 任务 `gmvmax.sync_campaigns` 落库 `TTBGmvMaxCampaign` 等）。这样能避免 Campaign 已被删除/迁移但页面仍使用旧缓存，确保后续指标同步、创意/产品关联查询能拿到最新 store_id、item_group_id 等信息。

### 同步成功后的查库请求

* `useGmvMaxMetricsSync` 在任务成功时调用 `queryClient.invalidateQueries` 失效 `composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId)` 下的缓存，促使正在展示的指标/创意/产品指标查询自动重新发起。刷新使用的 API 为 `GET /tenants/{workspaceId}/providers/{provider}/accounts/{authId}/gmvmax/{campaignId}/metrics`，会带上当前页面的 `start_date`、`end_date`、`level`、`campaign_ids`/`item_group_ids` 等查询参数读取最新入库结果。
* 如果你在页面上看到“同步成功”但网络面板没有出现上述 `GET .../metrics` 请求，通常是因为对应的 React Query 查询未处于“启用且已挂载”状态：
  * 进入页面后必须先满足启用条件（例如 `workspaceId`/`provider`/`authId`/`campaignId`、产品/创意标签页所需的 `campaignFilterId`/`itemGroupId` 等）。在条件未满足时 `useGmvMaxMetrics` 的 `enabled`/`creativeEnabled` 会为 `false`，`invalidateQueries` 只会标记缓存为 stale，但不会触发实际 refetch。
  * 切换到不展示指标的标签页或离开详情页后，对应查询会被卸载；此时即便任务完成也不会自动发起 `GET .../metrics`，需要回到指标/创意/产品标签页后 React Query 才会按 stale 缓存重新请求。

统一抽象：`src/hooks/useBackendTaskPolling.js` 负责所有后台任务状态轮询，只接受后端返回的 `status_url`，终态 (`SUCCESS`/`FAILURE`/`REVOKED`) 自动停止轮询并回调 `onSuccess`/`onFailure`。
