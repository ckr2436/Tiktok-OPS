# GMV Max 数据模型概览

本节整理了现有 Alembic 迁移中定义的 GMV Max 相关核心表，方便查找各层功能定位与唯一约束。

## 核心实体

- **`ttb_gmvmax_campaigns`**：GMV Max 广告系列主表，记录 workspace/auth 维度下的广告主、campaign_id、名称、状态、预算/币种以及外部创建/更新时间，保留原始 JSON；按 (workspace_id, auth_id, campaign_id) 唯一并索引广告主与状态字段，外键关联工作区与授权账号。
- **`ttb_gmvmax_campaign_products`**：维护系列与商品 (item_group_id) 的绑定关系以及门店 ID、operation_status，双重唯一约束避免同店商品重复投放或同系列重复绑定；外键指向系列主键 (campaign_pk) 以保证级联删除。
- **`ttb_gmvmax_strategy_config`**：存储系列层 ROI 目标、预算调节阈值、冷却时间等策略参数，(workspace_id, auth_id, campaign_id) 唯一，便于按工作区/授权/系列快速查找策略。
- **`ttb_gmvmax_creative_heating`**：创意加热/保温配置表，包含预算目标、步长、最长加热时长、最近动作与错误信息；按 workspace/provider/auth/campaign/creative 唯一并建立状态、系列、创意索引。
- **`ttb_gmvmax_campaign_sync_snapshots`**：存储从 TikTok 同步的系列原始快照及同步时间，联合唯一键覆盖 workspace/auth/advertiser/store/campaign/synced_at，便于审计历史同步。

## 指标与监控

- **`ttb_gmvmax_metrics_hourly`**：系列级小时聚合，含曝光、点击、花费/净花费、订单、GMV、ROI、商品曝光/点击、广告点击率/转化率、视频观看进度、直播观看/关注等；对 (campaign_id, interval_start) 唯一并索引系列与时间。
- **`ttb_gmvmax_metrics_daily`**：系列级按日汇总，字段与小时表一致且包含更新时间；(campaign_id, date) 唯一并索引日期与系列。
- **`ttb_gmvmax_creative_metrics_daily`**：创意维度日指标，记录创意/广告/商品 ID 与曝光、点击、成本、订单、ROI、点击率、转化率及各阶段视频观看率，联合唯一键覆盖 workspace/provider/auth/campaign/creative/stat_time_day，并提供系列、日期、创意索引。
- **`ttb_gmvmax_creative_metrics_10min`**：创意 10 分钟快照，包含广告主/系列/店铺/商品/创意维度与日粒度时间戳、曝光/点击/成本/订单/ROI、点击率、转化率、视频观看率、创意投放状态及原始指标 JSON；按 workspace/provider/auth/campaign/creative/stat_time_day/snapshot_at 唯一，并为系列/创意建立查询索引。
- **`ttb_gmvmax_action_logs`**：系列操作日志，记录动作类型、原因、前后配置、执行人、结果与错误，外键关联 workspace/auth/campaign 以支持级联删除并索引常用查询字段。

## 结构优化建议（对齐官方维度与指标）

- **拆分实体与指标表**：系列、商品、创意、直播间等静态属性集中在实体表中；时间序列指标拆分为 `gmv_<level>_metrics_<granularity>`（如 overview/campaign/product/creative/livestream/duration + daily/hourly/10min），减少宽表耦合。
- **补充新维度表**：新增概览级指标表（按 advertiser_id 汇总）、duration-level 指标表（按投放时长段聚合）、LIVE 直播间指标表（room_id 维度），以覆盖 API 支持的组合。
- **抽离创意静态字段**：将素材名称、授权类型、账号名等静态字段沉淀到创意实体表，指标表仅保留量化指标，并对 (campaign_id, creative_id, stat_time_day/hour/snapshot_at) 等维度施加唯一约束，避免重复写入。
- **为高频查询加索引**：典型过滤字段（promotion_type、advertiser_id、campaign_id、item_group_id、creative_id、stat_time_day/hour、snapshot_at、duration）建立联合索引；原始 JSON 大字段放在独立快照表表，防止影响主流程查询性能。

## 数据迁移与写入策略（ttb_* → gmv_*）

下表为空代表尚未完成回填。建议按“快照备份 → 结构化回填 → 双写灰度 → 停用旧表”的顺序迁移，保证业务不中断。

1. **准备阶段**
   - 锁定当前 Alembic 版本（0042_gmv_restructure_schema）并备份旧表 `ttb_gmvmax_%`（全库 `mysqldump --single-transaction --no-tablespaces --set-gtid-purged=OFF gmv ttb_gmvmax_% > backup.sql`）。
   - 开启事务隔离级别 `REPEATABLE READ`，避免回填过程中读取到半更新数据。

2. **一次性回填映射**
   - **系列实体**：`ttb_gmvmax_campaigns` → `gmv_campaigns`（promotion_type=PRODUCT，保持 currency/预算/roas_bid 等字段；ext_create_time/ext_update_time 对齐）。
   - **商品绑定**：`ttb_gmvmax_campaign_products` → `gmv_campaign_products`（promotion_type=PRODUCT，item_group_id 直接映射）。
   - **策略与加热**：`ttb_gmvmax_strategy_config` → `gmv_strategy_config`（新增 promotion_type 默认 PRODUCT）；`ttb_gmvmax_creative_heating` 保持不变，仅在需要时补充字段。
   - **指标数据**：
     - 系列日/小时：`ttb_gmvmax_metrics_daily/hourly` → `gmv_campaign_metrics_daily/hourly`（promotion_type=PRODUCT）。
     - 创意日/10 分钟：`ttb_gmvmax_creative_metrics_daily/10min` → `gmv_creative_metrics_daily/10min`。静态字段如素材名、授权类型可落地到 `gmv_creatives`，指标表仅保留量化字段。
   - 建议写一条迁移脚本（Python/SQL），以批次方式 `INSERT ... SELECT`，并记录迁移批次号与行数，用于对账。

3. **对账与校验**
   - 对比旧表与新表核心指标总和（cost_cents、orders、gross_revenue_cents）以及行数，确保无缺失：
     - 示例：`SELECT SUM(cost_cents), COUNT(*) FROM ttb_gmvmax_metrics_daily` 与 `gmv_campaign_metrics_daily` 同期比对。
   - 检查唯一约束冲突，确保 (workspace_id, auth_id, campaign_id, stat_time_day/hour) 等维度无重复。

4. **双写灰度**
   - 调整同步任务：新拉取的数据同时写入旧表与新表（可通过 feature flag 控制），观察 1–2 天无差异后再停止旧表写入。
   - 读路径可优先新表，保留旧表只读以便回滚。

5. **切换与清理**
   - 完成灰度后，停用旧表写入逻辑，并在代码中标记 `ttb_gmvmax_%` 为只读/待废弃。
   - 后续新数据统一写入 `gmv_*` 系列；旧表可保留一段时间（如 30–60 天）后归档或删除。

6. **LIVE 与 duration 数据**
   - LIVE GMV Max 的新数据直接进入 `gmv_campaigns`（promotion_type=LIVE）、`gmv_livestreams`、`gmv_livestream_metrics_%`，原有表无对应数据可跳过回填。
   - duration‑level 若历史未存储，可从切换日起开始采集，避免回填空洞。

通过以上步骤，可以在不丢失历史数据的前提下逐步迁移到新的表结构，后续所有实时/批量同步写入建议以 `gmv_*` 为主。

