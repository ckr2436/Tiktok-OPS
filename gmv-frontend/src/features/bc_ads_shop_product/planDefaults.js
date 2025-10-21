// src/features/bc_ads_shop_product/planDefaults.js

const BASE_PLANS = [
  {
    id: 'diagnosis',
    title: '基础诊断与目标拆解',
    objective: '建立店铺现状诊断与首月投放目标，确保运营动作聚焦在可衡量的指标上。',
    audience: '平台运营顾问 / 租户运营负责人',
    focus: '店铺现状、商品结构、投放账户准备',
    cadence: '上线前 3-5 天完成，之后按周复盘',
    keyActions: [
      '梳理类目、价格带与主推商品，确认盈亏底线与提效目标；',
      '核对店铺基础设施（支付、物流、客服、素材库）确保可用；',
      '对接 BC Ads 广告账户、像素及数据埋点，验证关键指标可追踪。',
    ],
    deliverables: [
      '店铺现状诊断报告（GMV、转化率、投产比等核心指标）；',
      '首月 GMV / 投放目标拆解表（按周拆解曝光、转化目标）；',
    ],
    metrics: ['GMV', '广告消耗', 'ROAS', '转化率'],
    notes: '建议由平台顾问审核模板后再下发租户，以保持执行口径一致。',
  },
  {
    id: 'traffic',
    title: '冷启动流量蓄水',
    objective: '构建多渠道投放计划，获取首波高意向人群并测试素材效率。',
    audience: '租户广告投放团队',
    focus: '短视频种草、直播引流、商品广告配置',
    cadence: '执行期约 2 周，每 3 天复盘',
    keyActions: [
      '配置 BC Ads 商品广告计划，区分新品与爆款，结合自动化出价；',
      '同步内容团队直播节奏，准备福利/话术脚本保证高峰转化；',
      '上线 DPA 重定向计划，覆盖加购、收藏人群并追踪转化漏斗。',
    ],
    deliverables: [
      '广告计划排期与预算明细；',
      '投放素材包（短视频、直播回放、主图等关键素材）；',
    ],
    metrics: ['CTR', 'CVR', '直播峰值在线', '加购率'],
    notes: '广告与直播节奏需同步规划，避免预算集中在低效时段。',
  },
  {
    id: 'growth',
    title: '复购加速与高潜 SKU 加推',
    objective: '通过分层运营提升复购率与客单价，沉淀高潜商品口碑。',
    audience: '运营 / 客服 / 会员团队',
    focus: '会员触达、复购激励、售后体验',
    cadence: '执行期约 3 周，每周一复盘',
    keyActions: [
      '搭建老客专属活动，结合券包与会员触达节奏；',
      '优化高潜 SKU 详情页与评价沉淀，提升转化信任度；',
      '配置售后关怀流程，跟踪投诉并形成标准化 SOP。',
    ],
    deliverables: [
      '复购激励活动方案与触达脚本；',
      '售后体验监控看板（退货率、差评率等）；',
    ],
    metrics: ['复购率', '客单价', '售后完结时长', '评价星级'],
    notes: '关注大促前后波动，必要时与平台共建联合营销节奏。',
  },
];

export const DEFAULT_SCHEDULE = Object.freeze({
  enabled: false,
  status: 'idle',
  taskName: 'bc-ads-plan-refresh',
  cron: '0 */6 * * *',
  timezone: 'Asia/Shanghai',
  description: '按固定频率刷新运营计划模板，保障租户自动获得最新版本。',
  lastRunAt: '',
  nextRunAt: '',
});

export const DEFAULT_PLAN_CONFIG = {
  syncCooldownMinutes: 45,
  plans: BASE_PLANS,
  schedule: DEFAULT_SCHEDULE,
};

function ensureArray(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item ?? '').trim()).filter(Boolean);
  }
  if (typeof value === 'string') {
    return value
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
}

function arrayToMultiline(value) {
  return ensureArray(value).join('\n');
}

function clampMinutes(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return DEFAULT_PLAN_CONFIG.syncCooldownMinutes;
  return Math.min(Math.max(num, 5), 1440);
}

function normalizeSchedule(rawSchedule = {}) {
  const schedule = rawSchedule?.schedule ?? rawSchedule ?? {};
  const enabled = Boolean(
    rawSchedule?.enabled ??
      rawSchedule?.is_enabled ??
      schedule?.enabled ??
      schedule?.is_enabled ??
      DEFAULT_SCHEDULE.enabled,
  );

  const taskName =
    schedule?.task_name ??
    schedule?.taskName ??
    schedule?.name ??
    schedule?.task_key ??
    DEFAULT_SCHEDULE.taskName;

  const cron =
    schedule?.cron ??
    schedule?.cron_expression ??
    schedule?.schedule ??
    schedule?.expression ??
    DEFAULT_SCHEDULE.cron;

  const timezone =
    schedule?.timezone ??
    schedule?.tz ??
    schedule?.time_zone ??
    DEFAULT_SCHEDULE.timezone;

  const description =
    schedule?.description ??
    schedule?.desc ??
    schedule?.note ??
    DEFAULT_SCHEDULE.description;

  const status = schedule?.status ?? schedule?.state ?? DEFAULT_SCHEDULE.status;

  const lastRunAt =
    schedule?.last_run_at ??
    schedule?.lastRunAt ??
    schedule?.latest_run_at ??
    schedule?.ran_at ??
    DEFAULT_SCHEDULE.lastRunAt;

  const nextRunAt =
    schedule?.next_run_at ??
    schedule?.nextRunAt ??
    schedule?.upcoming_run_at ??
    schedule?.scheduled_for ??
    DEFAULT_SCHEDULE.nextRunAt;

  return {
    enabled,
    status,
    taskName,
    cron,
    timezone,
    description,
    lastRunAt,
    nextRunAt,
  };
}

export function toEditableConfig(rawConfig = {}) {
  const fallback = DEFAULT_PLAN_CONFIG;
  const plans = Array.isArray(rawConfig?.plans) && rawConfig.plans.length > 0
    ? rawConfig.plans
    : fallback.plans;

  const editablePlans = plans.map((plan, idx) => {
    const preset = fallback.plans[idx] ?? fallback.plans[0];
    return {
      id: plan?.id ?? preset?.id ?? `plan-${idx + 1}`,
      title: plan?.title ?? preset?.title ?? `计划模块 ${idx + 1}`,
      objective: plan?.objective ?? preset?.objective ?? '',
      audience: plan?.audience ?? plan?.target_audience ?? preset?.audience ?? '',
      focus: plan?.focus ?? plan?.highlight ?? preset?.focus ?? '',
      cadence: plan?.cadence ?? plan?.frequency ?? preset?.cadence ?? '',
      keyActions: arrayToMultiline(plan?.key_actions ?? plan?.actions ?? preset?.keyActions),
      deliverables: arrayToMultiline(plan?.deliverables ?? preset?.deliverables),
      metrics: arrayToMultiline(plan?.metrics ?? plan?.kpis ?? preset?.metrics),
      notes: plan?.notes ?? preset?.notes ?? '',
    };
  });

  const cooldown = clampMinutes(rawConfig?.sync_cooldown_minutes ?? rawConfig?.syncCooldownMinutes);
  const schedule = normalizeSchedule(rawConfig?.schedule ?? rawConfig?.sync_schedule ?? {});

  return {
    syncCooldownMinutes: cooldown,
    plans: editablePlans,
    schedule,
  };
}

function arrayFromMultiline(text) {
  if (typeof text !== 'string') return [];
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export function toApiPayload(editableConfig = {}) {
  const plans = Array.isArray(editableConfig?.plans) ? editableConfig.plans : [];
  const schedule = editableConfig?.schedule ?? {};
  const timezone = String(schedule?.timezone ?? DEFAULT_SCHEDULE.timezone).trim();
  return {
    sync_cooldown_minutes: clampMinutes(editableConfig?.syncCooldownMinutes),
    plans: plans.map((plan, idx) => ({
      id: plan?.id || `plan-${idx + 1}`,
      title: String(plan?.title ?? `计划模块 ${idx + 1}`).trim(),
      objective: String(plan?.objective ?? '').trim(),
      audience: String(plan?.audience ?? '').trim(),
      focus: String(plan?.focus ?? '').trim(),
      cadence: String(plan?.cadence ?? '').trim(),
      key_actions: arrayFromMultiline(plan?.keyActions ?? ''),
      deliverables: arrayFromMultiline(plan?.deliverables ?? ''),
      metrics: arrayFromMultiline(plan?.metrics ?? ''),
      notes: String(plan?.notes ?? '').trim(),
    })),
    schedule: {
      enabled: Boolean(schedule?.enabled),
      task_name: String(schedule?.taskName ?? DEFAULT_SCHEDULE.taskName).trim(),
      cron: String(schedule?.cron ?? DEFAULT_SCHEDULE.cron).trim(),
      timezone: timezone || DEFAULT_SCHEDULE.timezone,
      description: String(schedule?.description ?? '').trim(),
    },
  };
}

export function toDisplayConfig(rawConfig = {}) {
  const fallback = DEFAULT_PLAN_CONFIG;
  const plans = Array.isArray(rawConfig?.plans) && rawConfig.plans.length > 0
    ? rawConfig.plans
    : fallback.plans;

  const normalizedPlans = plans.map((plan, idx) => {
    const preset = fallback.plans[idx] ?? fallback.plans[0];
    return {
      id: plan?.id ?? preset?.id ?? `plan-${idx + 1}`,
      title: plan?.title ?? preset?.title ?? `计划模块 ${idx + 1}`,
      objective: plan?.objective ?? preset?.objective ?? '',
      audience: plan?.audience ?? plan?.target_audience ?? preset?.audience ?? '',
      focus: plan?.focus ?? plan?.highlight ?? preset?.focus ?? '',
      cadence: plan?.cadence ?? plan?.frequency ?? preset?.cadence ?? '',
      keyActions: ensureArray(plan?.key_actions ?? plan?.actions ?? preset?.keyActions),
      deliverables: ensureArray(plan?.deliverables ?? preset?.deliverables),
      metrics: ensureArray(plan?.metrics ?? plan?.kpis ?? preset?.metrics),
      notes: plan?.notes ?? preset?.notes ?? '',
    };
  });

  const cooldown = clampMinutes(rawConfig?.sync_cooldown_minutes ?? rawConfig?.syncCooldownMinutes);
  const schedule = normalizeSchedule(rawConfig?.schedule ?? rawConfig?.sync_schedule ?? {});

  return {
    syncCooldownMinutes: cooldown,
    plans: normalizedPlans,
    schedule,
  };
}

export default DEFAULT_PLAN_CONFIG;
