// GMV Max 策略预设模板定义
// 这些模板用于在前端快速填充策略 JSON，便于运营和测试。

export const GMV_MAX_STRATEGY_PRESETS = {
  conservative: {
    name: 'Conservative (稳健控本)',
    description:
      '适合新品冷启动或预算有限场景，控成本为主，放量保守。',
    controls: {
      max_daily_budget_cents: 5000, // 50.00
      min_daily_budget_cents: 2000, // 20.00
      target_roi_min: 1.5,
      target_roi_max: 3.0,
      stop_loss: {
        enabled: true,
        roi_threshold: 1.2,
        spend_threshold_cents: 3000,
        action: 'PAUSE',
        reduce_budget_ratio: 0.5,
      },
      scale_up: {
        enabled: true,
        roi_threshold: 2.0,
        step_ratio: 0.2,
        max_step_daily: 2,
      },
    },
    monitoring: {
      interval_minutes: 60,
      lookback_hours: 24,
      use_hourly_metrics: true,
    },
  },

  balanced: {
    name: 'Balanced (平衡放量)',
    description:
      '在可接受 ROI 前提下争取更多曝光和订单，适合稳定放量阶段。',
    controls: {
      max_daily_budget_cents: 15000, // 150.00
      min_daily_budget_cents: 5000, // 50.00
      target_roi_min: 1.2,
      target_roi_max: 2.5,
      stop_loss: {
        enabled: true,
        roi_threshold: 1.0,
        spend_threshold_cents: 5000,
        action: 'SET_BUDGET',
        reduce_budget_ratio: 0.3,
      },
      scale_up: {
        enabled: true,
        roi_threshold: 1.8,
        step_ratio: 0.3,
        max_step_daily: 3,
      },
    },
    monitoring: {
      interval_minutes: 45,
      lookback_hours: 24,
      use_hourly_metrics: true,
    },
  },

  aggressive: {
    name: 'Aggressive (冲量)',
    description:
      '适合大促、清库存、爆品冲榜等场景，接受更低 ROI 换取更高 GMV。',
    controls: {
      max_daily_budget_cents: 30000, // 300.00
      min_daily_budget_cents: 10000, // 100.00
      target_roi_min: 0.8,
      target_roi_max: 2.0,
      stop_loss: {
        enabled: true,
        roi_threshold: 0.7,
        spend_threshold_cents: 8000,
        action: 'SET_BUDGET',
        reduce_budget_ratio: 0.3,
      },
      scale_up: {
        enabled: true,
        roi_threshold: 1.3,
        step_ratio: 0.4,
        max_step_daily: 4,
      },
    },
    monitoring: {
      interval_minutes: 30,
      lookback_hours: 12,
      use_hourly_metrics: true,
    },
  },
};

export const GMV_MAX_STRATEGY_PRESET_LIST = Object.entries(
  GMV_MAX_STRATEGY_PRESETS,
).map(([key, value]) => ({
  key,
  name: value.name,
  description: value.description,
}));
