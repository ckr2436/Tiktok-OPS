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

