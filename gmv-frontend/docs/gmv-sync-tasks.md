# GMV Ops 同步任务梳理

下表列出了前端会触发异步后端任务并轮询状态的场景，覆盖 GMV Max 相关页面。

| 场景/页面 | 触发接口 | 状态查询 | 前端封装与调用 | 说明 |
| --- | --- | --- | --- | --- |
| GMV Max 总览页（系列/店铺列表、概览指标刷新） | `POST /tenants/{wid}/gmvmax/sync`（`startGmvMaxSync`，携带 provider、auth_id 与 scope 参数） | 后端返回的 `status_url`（形如 `/tenants/{wid}/gmvmax/tasks/{task_id}`） | `useGmvSyncTask` 在 `GmvMaxOverviewPage.jsx` 中创建任务并通过 `useBackendTaskPolling` 轮询；`useEnsureFreshGmvData` 复用同一套逻辑用于详情页预取 | 终态停止轮询并刷新 `composeMetricsQueryBaseKey(..., 'all')` 下的 React Query 缓存 |
| GMV Max Campaign 详情页（指标/创意/产品指标刷新） | `POST /tenants/{wid}/providers/{provider}/accounts/{authId}/gmvmax/{campaignId}/metrics/sync`（`syncGmvMaxMetrics`） | 同步返回的 `status_url`（同样指向 `/tenants/.../gmvmax/tasks/{task_id}`） | `useGmvMaxMetricsSync` 在 `GmvMaxCampaignDetailPage.jsx` 中触发，内部使用 `useBackendTaskPolling` 轮询任务终态并刷新 `composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId)` | 终态刷新当前 Campaign 的指标查询；失败时提示“GMV Max 数据同步失败，请稍后重试。” |

统一抽象：`src/hooks/useBackendTaskPolling.js` 负责所有后台任务状态轮询，只接受后端返回的 `status_url`，终态 (`SUCCESS`/`FAILURE`/`REVOKED`) 自动停止轮询并回调 `onSuccess`/`onFailure`。
