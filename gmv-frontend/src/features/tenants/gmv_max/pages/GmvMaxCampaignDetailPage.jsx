import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { message } from 'antd';
import { useQueryClient } from '@tanstack/react-query';

import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';
import Modal from '@/components/ui/Modal.jsx';

import {
  useApplyGmvMaxActionMutation,
  useStartGmvMaxCreativeHeatingMutation,
  useStopGmvMaxCreativeHeatingMutation,
  useGmvMaxCreativeAssetsQuery,
  useGmvMaxCampaignQuery,
  useAccountsQuery,
  useAdvertisersQuery,
  useStoresQuery,
  useProductsQuery,
  useGmvMaxCreativeHeatingQuery,
  useGmvMaxStrategyQuery,
  usePreviewGmvMaxStrategyMutation,
  useUpdateGmvMaxStrategyMutation,
  composeMetricsQueryBaseKey,
} from '../hooks/gmvMaxQueries.js';
import { useEnsureFreshGmvData } from '../hooks/useGmvSyncTask.js';
import { useGmvMaxMetrics } from '../hooks/useGmvMaxMetrics.js';
import { useGmvMaxMetricsSync } from '../hooks/useGmvMaxMetricsSync.js';
import useGmvMaxNotifications from '../hooks/useGmvMaxNotifications.js';
import { GmvMaxTexts } from '../locale.js';
import ActionLogsTable from '../components/ActionLogsTable.jsx';
import {
  formatRangeAsDateStrings,
  getAdvertiserRecentRange,
  getAdvertiserTodayRange,
  resolveTimezoneLabel,
} from '../utils/timezone.js';
import ProductSelectionPanel from './gmvMaxOverview/ProductSelectionPanel.jsx';
import {
  DEFAULT_REPORT_METRICS,
  getProductIdentifier,
  getStoreLabel,
  isCampaignDeleted,
  ensureArray,
} from './gmvMaxOverview/helpers.js';
import { normalizeTaskState } from '../utils/taskState.js';

const ALL_CREATIVE_STATUS_KEYS = [
  'DELIVERING',
  'LEARNING',
  'IN_QUEUE',
  'AUTHORIZATION_NEEDED',
  'NOT_ACTIVE',
  'NOT_DELIVERING',
  'EXCLUDED',
  'REJECTED',
  'UNAVAILABLE',
  'CANDIDATE',
];

const MIN_MONITORING_INTERVAL = 3;
const METRIC_CHOICES = [
  { value: 'roi', label: 'ROAS' },
  { value: 'spend', label: '消耗' },
  { value: 'gmv', label: 'GMV' },
  { value: 'orders', label: '订单' },
  { value: 'ctr', label: 'CTR' },
  { value: 'cpc', label: 'CPC' },
];
const TREND_METRIC_OPTIONS = [
  { key: 'spend', label: '消耗', color: '#2563eb', money: true },
  { key: 'gmv', label: 'GMV', color: '#059669', money: true },
  { key: 'orders', label: '订单', color: '#d97706' },
  { key: 'roas', label: 'ROAS', color: '#7c3aed' },
];
const CREATIVE_SORT_OPTIONS = [
  { value: 'spend', label: '消耗' },
  { value: 'gmv', label: 'GMV' },
  { value: 'orders', label: '转化' },
  { value: 'roas', label: 'ROAS' },
  { value: 'ctr', label: 'CTR' },
  { value: 'cpc', label: 'CPC' },
  { value: 'cpm', label: 'CPM' },
  { value: 'impressions', label: '曝光' },
  { value: 'clicks', label: '点击' },
];
const HEATABLE_CREATIVE_STATUSES = new Set(['DELIVERING', 'LEARNING', 'IN_QUEUE']);
const SEED_MIN_CONVERSIONS = 3;
const SEED_MIN_ROAS = 2;
const SEED_MIN_SPEND = 20;
const SEED_RULE_TEXT = `满足转化≥${SEED_MIN_CONVERSIONS}、ROAS≥${SEED_MIN_ROAS.toFixed(1)}、消耗≥$${SEED_MIN_SPEND}`;
const OPERATOR_CHOICES = [
  { value: '>', label: '>' },
  { value: '>=', label: '≥' },
  { value: '<', label: '<' },
  { value: '<=', label: '≤' },
];
const ACTION_CHOICES = [
  { value: 'pause', label: '暂停系列' },
  { value: 'resume', label: '启用系列' },
  { value: 'increase_budget', label: '提升预算 %' },
  { value: 'decrease_budget', label: '降低预算 %' },
];

function formatNumber(value, options = {}) {
  if (value === undefined || value === null || Number.isNaN(value)) return '—';
  return Number(value).toLocaleString(undefined, options);
}

function formatPercent(value) {
  if (value === undefined || value === null || Number.isNaN(value)) return '—';
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function formatMoney(value) {
  if (value === undefined || value === null || Number.isNaN(value)) return '—';
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatRoas(value) {
  if (value === null || value === undefined || value === '') return '—';
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return num.toFixed(2);
}

function formatRate(value) {
  if (value === null || value === undefined || value === '') return '—';
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return `${num.toFixed(num >= 10 ? 1 : 2)}%`;
}

function formatDataFreshness(freshness) {
  if (!freshness) return null;
  const seconds = Number(freshness.age_seconds);
  const age = Number.isFinite(seconds)
    ? seconds < 60
      ? `${Math.max(0, Math.round(seconds))} 秒`
      : `${Math.max(1, Math.round(seconds / 60))} 分钟`
    : '未知';
  const labels = {
    fresh: '新鲜',
    stale: '已滞后',
    missing: '无快照',
    historical: '历史完整日',
  };
  const rawSource = String(freshness.source || '');
  const sourceLabel = rawSource.includes('gmv_creative_metrics_10min')
    ? '素材实时数据'
    : rawSource.includes('creative')
      ? '素材日级数据'
      : rawSource.includes('campaign')
        ? '系列日级数据'
        : rawSource.includes('overview')
          ? '整体投放数据'
          : '数据库快照';
  return {
    state: freshness.state || 'missing',
    label: labels[freshness.state] || '未知',
    detail: `${sourceLabel} · ${age}前更新`,
  };
}

function formatChineseDate(value) {
  const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!match) return value || '—';
  return `${match[1]}年${Number(match[2])}月${Number(match[3])}日`;
}

function formatDateRangeLabel(range) {
  const start = range?.start_date;
  const end = range?.end_date;
  if (!start || !end) return '统计日期待确定';
  if (start === end) return `统计日期：${formatChineseDate(start)}`;
  return `统计范围：${formatChineseDate(start)} — ${formatChineseDate(end)}`;
}

function extractResourceItems(data) {
  return ensureArray(data?.items ?? data?.list ?? data?.data?.items ?? data?.data?.list ?? data);
}

function readableResourceName(value, id) {
  const normalized = String(value || '').trim();
  if (!normalized || normalized === String(id || '') || /^\d+$/.test(normalized)) return '';
  return normalized;
}

function truncateText(value, maxLength = 42) {
  const text = value === undefined || value === null ? '' : String(value);
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 1)).trim()}…`;
}

function parseOptionalNumber(value) {
  if (value === '' || value === undefined || value === null) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parseOptionalInteger(value) {
  if (value === '' || value === undefined || value === null) return undefined;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parseCreativeMetrics(metrics) {
  if (!metrics || typeof metrics !== 'object') {
    return {
      impressions: 0,
      clicks: 0,
      spend: 0,
      gmv: 0,
      orders: 0,
      ctr: 0,
      productClickRate: null,
      adClickRate: null,
      video2sRate: null,
      video6sRate: null,
      video25Rate: null,
      video50Rate: null,
	      video75Rate: null,
	      completionRate: null,
	      cpm: 0,
	      adFlowShare: null,
	      organicFlowShare: null,
	      cpc: 0,
	      conversionRate: null,
	      roas: 0,
	    };
  }
  const resolvedMetrics = metrics.metrics ?? metrics.metrics_data ?? metrics.metricsData ?? metrics;
  const spend = Number(resolvedMetrics.cost ?? resolvedMetrics.net_cost ?? resolvedMetrics.spend ?? 0) || 0;
  const gmv =
    Number(resolvedMetrics.gross_revenue ?? resolvedMetrics.gmv ?? resolvedMetrics.revenue ?? 0) || 0;
  const clicks =
    Number(
      resolvedMetrics.product_clicks ?? resolvedMetrics.clicks ?? resolvedMetrics.total_clicks ?? resolvedMetrics.ad_clicks ?? 0,
    ) || 0;
  const impressions =
    Number(resolvedMetrics.product_impressions ?? resolvedMetrics.impressions ?? resolvedMetrics.views ?? 0) || 0;
  const orders = Number(resolvedMetrics.orders ?? resolvedMetrics.total_orders ?? resolvedMetrics.conversions ?? 0) || 0;
  const ctr =
    resolvedMetrics.product_click_rate ??
    resolvedMetrics.ad_click_rate ??
    resolvedMetrics.ctr ??
    resolvedMetrics.click_through_rate ??
    (impressions > 0 ? (clicks / impressions) * 100 : 0);
  const productClickRate =
    resolvedMetrics.product_click_rate ?? (impressions > 0 ? (clicks / impressions) * 100 : null);
  const adClickRate = resolvedMetrics.ad_click_rate ?? resolvedMetrics.ctr ?? resolvedMetrics.click_through_rate ?? null;
  const video2sRate = resolvedMetrics.ad_video_view_rate_2s ?? resolvedMetrics.video_view_rate_2s ?? null;
  const video6sRate = resolvedMetrics.ad_video_view_rate_6s ?? resolvedMetrics.video_view_rate_6s ?? null;
  const video25Rate =
    resolvedMetrics.ad_video_view_rate_p25 ?? resolvedMetrics.video_view_rate_25 ?? resolvedMetrics.video_view_rate_p25 ?? null;
  const video50Rate =
    resolvedMetrics.ad_video_view_rate_p50 ?? resolvedMetrics.video_view_rate_50 ?? resolvedMetrics.video_view_rate_p50 ?? null;
  const video75Rate =
    resolvedMetrics.ad_video_view_rate_p75 ?? resolvedMetrics.video_view_rate_75 ?? resolvedMetrics.video_view_rate_p75 ?? null;
  const completionRate =
    resolvedMetrics.ad_video_view_rate_p100 ??
    resolvedMetrics.video_view_rate_100 ??
    resolvedMetrics.video_view_rate_p100 ??
    resolvedMetrics.completion_rate ??
    null;
	  const cpc = resolvedMetrics.cpc ?? resolvedMetrics.cost_per_click ?? (clicks > 0 ? spend / clicks : 0);
	  const cpm = resolvedMetrics.cpm ?? resolvedMetrics.cost_per_mille ?? (impressions > 0 ? (spend / impressions) * 1000 : 0);
	  const adConversionRateRaw =
	    resolvedMetrics.ad_conversion_rate ??
	    resolvedMetrics.conversion_rate ??
	    resolvedMetrics.cvr ??
	    resolvedMetrics.ad_cvr ??
	    null;
	  const adConversionRateNumber = Number(adConversionRateRaw);
	  const adConversionRate =
	    adConversionRateRaw === null || adConversionRateRaw === undefined || adConversionRateRaw === '' ||
	    !Number.isFinite(adConversionRateNumber)
	      ? null
	      : Math.max(0, adConversionRateNumber);
	  const estimatedAdOrders = adConversionRate === null ? null : clicks * (adConversionRate / 100);
	  const adFlowShareFraction =
	    orders > 0 && estimatedAdOrders !== null ? Math.min(1, Math.max(0, estimatedAdOrders / orders)) : null;
	  const adFlowShare = adFlowShareFraction === null ? null : adFlowShareFraction * 100;
	  const organicFlowShare = adFlowShareFraction === null ? null : (1 - adFlowShareFraction) * 100;
	  const roasValue = resolvedMetrics.roas ?? resolvedMetrics.roi ?? (spend > 0 ? gmv / spend : 0);
	  const roas = Number(roasValue) || 0;
	  return {
    impressions,
    clicks,
    spend,
    gmv,
    orders,
    ctr,
    productClickRate,
    adClickRate,
    video2sRate,
    video6sRate,
    video25Rate,
    video50Rate,
	    video75Rate,
	    completionRate,
	    cpm,
	    adFlowShare,
	    organicFlowShare,
	    cpc,
	    conversionRate: adConversionRate,
	    roas,
	  };
}

function mergeCreativeMetrics(current, incoming) {
  const left = current || parseCreativeMetrics({});
  const right = incoming || parseCreativeMetrics({});
  const impressions = (Number(left.impressions) || 0) + (Number(right.impressions) || 0);
  const clicks = (Number(left.clicks) || 0) + (Number(right.clicks) || 0);
  const spend = (Number(left.spend) || 0) + (Number(right.spend) || 0);
  const gmv = (Number(left.gmv) || 0) + (Number(right.gmv) || 0);
  const orders = (Number(left.orders) || 0) + (Number(right.orders) || 0);
  const weightedRate = (key, leftWeight, rightWeight) => {
    const leftValue = Number(left[key]);
    const rightValue = Number(right[key]);
    let numerator = 0;
    let denominator = 0;
    if (left[key] !== null && left[key] !== undefined && Number.isFinite(leftValue) && leftWeight > 0) {
      numerator += leftValue * leftWeight;
      denominator += leftWeight;
    }
    if (right[key] !== null && right[key] !== undefined && Number.isFinite(rightValue) && rightWeight > 0) {
      numerator += rightValue * rightWeight;
      denominator += rightWeight;
    }
    return denominator > 0 ? numerator / denominator : null;
  };
  const merged = {
    ...left,
    impressions,
    clicks,
    spend,
    gmv,
    orders,
    ctr: impressions > 0 ? (clicks / impressions) * 100 : 0,
    cpc: clicks > 0 ? spend / clicks : 0,
    cpm: impressions > 0 ? (spend / impressions) * 1000 : 0,
    roas: spend > 0 ? gmv / spend : 0,
    conversionRate: clicks > 0 ? (orders / clicks) * 100 : null,
  };
  ['productClickRate', 'adClickRate', 'video2sRate', 'video6sRate', 'video25Rate', 'video50Rate', 'video75Rate', 'completionRate']
    .forEach((key) => {
      merged[key] = weightedRate(
        key,
        Number(left.impressions) || 0,
        Number(right.impressions) || 0,
      );
    });
  merged.adFlowShare = weightedRate(
    'adFlowShare',
    Number(left.orders) || 0,
    Number(right.orders) || 0,
  );
  merged.organicFlowShare = weightedRate(
    'organicFlowShare',
    Number(left.orders) || 0,
    Number(right.orders) || 0,
  );
  return merged;
}

function summarizeCreativeMetrics(creatives) {
  const totals = {
    impressions: 0,
    clicks: 0,
    spend: 0,
    gmv: 0,
    orders: 0,
  };
  const weightedRates = {
    video2sRate: { numerator: 0, denominator: 0 },
    video6sRate: { numerator: 0, denominator: 0 },
    completionRate: { numerator: 0, denominator: 0 },
  };
  let estimatedAdOrders = 0;
  let conversionRateClicks = 0;

  ensureArray(creatives).forEach((creative) => {
    const metrics = creative?.metrics || {};
    const impressions = Number(metrics.impressions) || 0;
    const clicks = Number(metrics.clicks) || 0;
    const orders = Number(metrics.orders) || 0;
    totals.impressions += impressions;
    totals.clicks += clicks;
    totals.spend += Number(metrics.spend) || 0;
    totals.gmv += Number(metrics.gmv) || 0;
    totals.orders += orders;

    const rawConversionRate = metrics.conversionRate;
    const conversionRate = Number(rawConversionRate);
    if (
      clicks > 0 &&
      rawConversionRate !== null &&
      rawConversionRate !== undefined &&
      rawConversionRate !== '' &&
      Number.isFinite(conversionRate)
    ) {
      estimatedAdOrders += clicks * (Math.max(0, conversionRate) / 100);
      conversionRateClicks += clicks;
    }

    Object.keys(weightedRates).forEach((key) => {
      const rawRate = metrics[key];
      const rate = Number(rawRate);
      if (
        impressions > 0 &&
        rawRate !== null &&
        rawRate !== undefined &&
        rawRate !== '' &&
        Number.isFinite(rate)
      ) {
        weightedRates[key].numerator += impressions * rate;
        weightedRates[key].denominator += impressions;
      }
    });
  });

  const rateValue = (key) => {
    const aggregate = weightedRates[key];
    return aggregate.denominator > 0 ? aggregate.numerator / aggregate.denominator : null;
  };
  const adFlowShare =
    totals.orders > 0 && conversionRateClicks > 0
      ? Math.min(100, Math.max(0, (estimatedAdOrders / totals.orders) * 100))
      : null;

  return {
    ...totals,
    cpm: totals.impressions > 0 ? (totals.spend / totals.impressions) * 1000 : 0,
    cpc: totals.clicks > 0 ? totals.spend / totals.clicks : 0,
    roas: totals.spend > 0 ? totals.gmv / totals.spend : 0,
    ctr: totals.impressions > 0 ? (totals.clicks / totals.impressions) * 100 : 0,
    conversionRate: totals.clicks > 0 ? (totals.orders / totals.clicks) * 100 : null,
    video2sRate: rateValue('video2sRate'),
    video6sRate: rateValue('video6sRate'),
    completionRate: rateValue('completionRate'),
    adFlowShare,
    organicFlowShare: adFlowShare === null ? null : 100 - adFlowShare,
  };
}

function getMetricValue(entry, key) {
  if (!entry) return 0;
  const metrics = entry.metrics || entry;
  const normalizedKey = String(key || '').toLowerCase();
  const aliasMap = {
    spend: ['cost', 'net_cost', 'spend'],
    gmv: ['gross_revenue', 'gmv', 'revenue', 'total_gross_revenue'],
    clicks: ['product_clicks', 'clicks', 'total_clicks', 'ad_clicks'],
    impressions: ['product_impressions', 'impressions', 'views'],
    ctr: ['product_click_rate', 'ad_click_rate', 'ctr', 'click_through_rate'],
  };
  const candidates = aliasMap[normalizedKey] || [key];
  const value = candidates.reduce((result, candidate) => {
    if (result !== undefined && result !== null) return result;
    const candidateKey = candidate || key;
    return (
      metrics[candidateKey] ??
      metrics[candidateKey?.toUpperCase?.()] ??
      metrics[candidateKey?.toLowerCase?.()] ??
      metrics[`total_${candidateKey}`] ??
      metrics[`total${candidateKey?.charAt?.(0)?.toUpperCase?.()}${candidateKey?.slice?.(1)}`]
    );
  }, undefined);
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : 0;
}

function extractDateLabel(entry) {
  if (!entry) return '';
  const dimensions = entry.dimensions || entry.dimension || {};
  return (
    entry.stat_time_day ||
    dimensions.stat_time_day ||
    dimensions.date ||
    dimensions.interval_start ||
    entry.date ||
    entry.interval_start ||
    entry.intervalStart ||
    entry.stat_time ||
    entry.period ||
    ''
  );
}

function computeTimeRange(range, customRange, timeZone) {
  const toDate = (value) => {
    if (value instanceof Date) return value;
    if (!value) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  };

  if (range === 'custom' && customRange?.start && customRange?.end) {
    const normalizedStart = formatRangeAsDateStrings({
      start: toDate(customRange.start),
      timeZone,
    }).start_date;
    const normalizedEnd = formatRangeAsDateStrings({
      end: toDate(customRange.end),
      timeZone,
    }).end_date;
    return { start_date: normalizedStart, end_date: normalizedEnd };
  }
  if (range === 'today') {
    return formatRangeAsDateStrings(getAdvertiserTodayRange(timeZone));
  }
  const days = range === '30d' ? 30 : 7;
  const rangeDates = getAdvertiserRecentRange(days, timeZone);
  return formatRangeAsDateStrings(rangeDates);
}

function isMissingFilterError(error) {
  if (!error) return false;
  const status = error?.response?.status || error?.status;
  if (status !== 422) return false;
  const message = String(
    error?.response?.data?.message || error?.response?.data?.detail || error?.message || '',
  ).toLowerCase();
  return message.includes('campaign_id') || message.includes('item_group_id');
}

function isCancelledRequest(error) {
  if (!error) return false;
  if (error.__cancelledRequest) return true;
  const code = error.code || error?.response?.code;
  if (code === 'ERR_CANCELED') return true;
  const name = error.name || error?.response?.name;
  if (name === 'AbortError' || name === 'CanceledError') return true;
  return false;
}

function resolveMetricsError(error, defaultMessage = '数据加载失败') {
  if (!error) return '';

  if (isCancelledRequest(error)) {
    return '';
  }

  if (isMissingFilterError(error)) {
    return '暂无数据，请检查广告系列和商品组配置。';
  }
  const status = error?.response?.status;
  if (error?.code === 'ECONNABORTED' || (status && status >= 500)) {
    return '报表同步中或暂时不可用，请稍后再试。';
  }
  return error?.response?.data?.message || error?.message || defaultMessage;
}

function resolveActionError(error, defaultMessage) {
  const detail = error?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object' && detail.message) return detail.message;
  return error?.response?.data?.message || error?.message || defaultMessage;
}

function normalizeStrategyResponse(data) {
  if (!data || typeof data !== 'object') {
    return {
      enabled: true,
      autoHeatingEnabled: true,
      cooldownMinutes: 60,
      monitorIntervalMinutes: 3,
      evaluationWindowMinutes: 60,
      minSpendDollars: 3,
      pacingEnabled: true,
      hermesEnabled: false,
      minRuntimeMinutes: 30,
      thresholds: {},
      rules: [],
      smartGuardState: {},
      raw: data,
    };
  }
  const config = data.config_json || data.configJson || {};
  const smartGuard = config.smart_guard || config.smartGuard || {};
  const smartGuardState = config.smart_guard_state || config.smartGuardState || {};
  const rules = ensureArray(config.rules).map((rule, index) => ({
    id: rule?.id || `rule-${index}`,
    metric: rule?.metric || 'roi',
    operator: rule?.operator || '>',
    value: rule?.value ?? '',
    secondaryMetric: rule?.secondaryMetric || '',
    secondaryOperator: rule?.secondaryOperator || '>',
    secondaryValue: rule?.secondaryValue ?? '',
    conjunction: rule?.conjunction || 'AND',
    action: rule?.action || 'pause',
    actionValue: rule?.actionValue ?? '',
  }));
  const normalizedThresholds = {
    target_roi: data.target_roi ?? data.targetRoi ?? '',
    min_roi: data.min_roi ?? data.minRoi ?? '',
    max_roi: data.max_roi ?? data.maxRoi ?? '',
    min_impressions: data.min_impressions ?? data.minImpressions ?? '',
    min_clicks: data.min_clicks ?? data.minClicks ?? '',
    max_budget_raise_pct_per_day:
      data.max_budget_raise_pct_per_day ?? data.maxBudgetRaisePctPerDay ?? '',
    max_budget_cut_pct_per_day:
      data.max_budget_cut_pct_per_day ?? data.maxBudgetCutPctPerDay ?? '',
    max_roas_step_per_adjust:
      data.max_roas_step_per_adjust ?? data.maxRoasStepPerAdjust ?? '',
    auto_pause_roi_threshold:
      data.auto_pause_roi_threshold ??
      config.auto_pause_roi_threshold ??
      smartGuard.min_roi ??
      smartGuard.minRoi ??
      '',
  };
  return {
    enabled: Boolean(data.enabled ?? true),
    autoHeatingEnabled: Boolean(
      data.auto_heating_enabled ?? config.auto_heating_enabled ?? data.autoHeatingEnabled ?? true,
    ),
    cooldownMinutes:
      data.cooldown_minutes ??
      data.cooldownMinutes ??
      smartGuard.pause_cooldown_minutes ??
      smartGuard.pauseCooldownMinutes ??
      60,
    monitorIntervalMinutes:
      smartGuard.monitor_interval_minutes ?? smartGuard.monitorIntervalMinutes ?? 3,
    evaluationWindowMinutes:
      smartGuard.evaluation_window_minutes ?? smartGuard.evaluationWindowMinutes ?? 60,
    minSpendDollars:
      smartGuard.min_spend_dollars ??
      smartGuard.minSpendDollars ??
      (smartGuard.min_spend_cents || smartGuard.minSpendCents
        ? Number(smartGuard.min_spend_cents || smartGuard.minSpendCents) / 100
        : 3),
    pacingEnabled: Boolean(
      smartGuard.daily_budget_pacing ?? smartGuard.dailyBudgetPacing ?? true,
    ),
    hermesEnabled: Boolean(
      smartGuard.hermes_enabled ?? smartGuard.hermesEnabled ?? config.hermes_enabled ?? false,
    ),
    minRuntimeMinutes:
      data.min_runtime_minutes_before_first_change ??
      data.minRuntimeMinutesBeforeFirstChange ??
      30,
    thresholds: normalizedThresholds,
    rules,
    smartGuardState,
    raw: data,
  };
}

function createEmptyRule() {
  return {
    id: `rule-${Date.now()}`,
    metric: 'roi',
    operator: '>',
    value: '',
    secondaryMetric: '',
    secondaryOperator: '>',
    secondaryValue: '',
    conjunction: 'AND',
    action: 'pause',
    actionValue: '',
  };
}

function buildStrategyPayload(draft) {
  const { thresholds = {} } = draft || {};
  const payload = {
    enabled: Boolean(draft.enabled),
    auto_heating_enabled: Boolean(draft.autoHeatingEnabled),
    cooldown_minutes: Math.max(
      MIN_MONITORING_INTERVAL,
      Number.parseInt(draft.cooldownMinutes, 10) || MIN_MONITORING_INTERVAL,
    ),
    min_runtime_minutes_before_first_change: parseOptionalInteger(draft.minRuntimeMinutes),
    target_roi: parseOptionalNumber(thresholds.target_roi),
    min_roi: parseOptionalNumber(thresholds.min_roi),
    max_roi: parseOptionalNumber(thresholds.max_roi),
    min_impressions: parseOptionalInteger(thresholds.min_impressions),
    min_clicks: parseOptionalInteger(thresholds.min_clicks),
    auto_pause_roi_threshold: parseOptionalNumber(thresholds.auto_pause_roi_threshold),
    max_budget_raise_pct_per_day: parseOptionalNumber(thresholds.max_budget_raise_pct_per_day),
    max_budget_cut_pct_per_day: parseOptionalNumber(thresholds.max_budget_cut_pct_per_day),
    max_roas_step_per_adjust: parseOptionalNumber(thresholds.max_roas_step_per_adjust),
    config_json: {
      smart_guard: {
        enabled: Boolean(draft.enabled),
        monitor_interval_minutes: Math.max(
          MIN_MONITORING_INTERVAL,
          Number.parseInt(draft.monitorIntervalMinutes, 10) || MIN_MONITORING_INTERVAL,
        ),
        evaluation_window_minutes: Math.max(
          MIN_MONITORING_INTERVAL,
          Number.parseInt(draft.evaluationWindowMinutes, 10) || 60,
        ),
        pause_cooldown_minutes: Math.max(
          MIN_MONITORING_INTERVAL,
          Number.parseInt(draft.cooldownMinutes, 10) || 60,
        ),
        min_roi: parseOptionalNumber(thresholds.auto_pause_roi_threshold ?? thresholds.min_roi),
        min_spend_cents: Math.round((parseOptionalNumber(draft.minSpendDollars) ?? 3) * 100),
        daily_budget_pacing: Boolean(draft.pacingEnabled),
        hermes_enabled: Boolean(draft.hermesEnabled),
      },
      rules: (draft.rules || []).map((rule) => ({
        id: rule.id,
        metric: rule.metric,
        operator: rule.operator,
        value: rule.value,
        secondaryMetric: rule.secondaryMetric,
        secondaryOperator: rule.secondaryOperator,
        secondaryValue: rule.secondaryValue,
        conjunction: rule.conjunction,
        action: rule.action,
        actionValue: rule.actionValue,
      })),
    },
  };
  if (!payload.min_runtime_minutes_before_first_change) {
    delete payload.min_runtime_minutes_before_first_change;
  }
  return payload;
}

function summarizeMetrics(report) {
  const summary = report?.summary;
  if (
    summary &&
    typeof summary === 'object' &&
    ['spend', 'cost', 'gmv', 'gross_revenue', 'orders'].some((key) =>
      Object.prototype.hasOwnProperty.call(summary, key),
    )
  ) {
    const entry = { metrics: summary };
    const ctr = getMetricValue(entry, 'ctr');
    const cpc = getMetricValue(entry, 'cpc');
    const cpm = getMetricValue(entry, 'cpm');
    return {
      spend: getMetricValue(entry, 'spend'),
      gmv: getMetricValue(entry, 'gmv'),
      orders: getMetricValue(entry, 'orders'),
      ctrValues: ctr > 0 ? [ctr] : [],
      cpcValues: cpc > 0 ? [cpc] : [],
      cpmValues: cpm > 0 ? [cpm] : [],
    };
  }

  const entries = ensureArray(report?.list);
  return entries.reduce(
    (acc, entry) => {
      acc.spend += getMetricValue(entry, 'spend');
      acc.gmv += getMetricValue(entry, 'gmv');
      acc.orders += getMetricValue(entry, 'orders');
      const ctr = getMetricValue(entry, 'ctr');
      if (!Number.isNaN(ctr) && ctr > 0) {
        acc.ctrValues.push(ctr);
      }
      const cpc = getMetricValue(entry, 'cpc');
      if (!Number.isNaN(cpc) && cpc > 0) {
        acc.cpcValues.push(cpc);
      }
      const cpm = getMetricValue(entry, 'cpm');
      if (!Number.isNaN(cpm) && cpm > 0) {
        acc.cpmValues.push(cpm);
      }
      return acc;
    },
    { spend: 0, gmv: 0, orders: 0, ctrValues: [], cpcValues: [], cpmValues: [] },
  );
}

function buildTrendSeries(report, range) {
  const entries = ensureArray(report?.list);
  const byDate = new Map();
  entries.forEach((entry) => {
    const label = String(extractDateLabel(entry) || '').slice(0, 10);
    if (!label) return;
    const current = byDate.get(label) || { label, spend: 0, gmv: 0, orders: 0, roas: 0 };
    current.spend += getMetricValue(entry, 'spend');
    current.gmv += getMetricValue(entry, 'gmv');
    current.orders += getMetricValue(entry, 'orders');
    byDate.set(label, current);
  });

  const start = String(range?.start_date || '').slice(0, 10);
  const end = String(range?.end_date || '').slice(0, 10);
  if (start && end) {
    const cursor = new Date(`${start}T00:00:00Z`);
    const endDate = new Date(`${end}T00:00:00Z`);
    while (cursor <= endDate) {
      const label = cursor.toISOString().slice(0, 10);
      if (!byDate.has(label)) {
        byDate.set(label, { label, spend: 0, gmv: 0, orders: 0, roas: 0 });
      }
      cursor.setUTCDate(cursor.getUTCDate() + 1);
    }
  }

  return Array.from(byDate.values())
    .map((point) => ({
      ...point,
      roas: point.spend > 0 ? point.gmv / point.spend : 0,
    }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

function buildDimensionTable(report, dimensionKey, extraKeys = []) {
  const entries = ensureArray(report?.list);
  const groups = new Map();
  for (const entry of entries) {
    const dimensions = entry.dimensions || entry.dimension || {};
    const metrics = entry.metrics || entry || {};
    const key =
      dimensions[dimensionKey] ||
      dimensions[`${dimensionKey}_id`] ||
      metrics[dimensionKey] ||
      metrics[`${dimensionKey}_id`] ||
      'unknown';
    const name =
      metrics.product_name ||
      metrics.title ||
      dimensions[`${dimensionKey}_name`] ||
      dimensions.name ||
      dimensions.title ||
      String(key);
    if (!groups.has(key)) {
      groups.set(key, {
        id: key,
        name,
        spend: 0,
        gmv: 0,
        orders: 0,
        ctr: 0,
        clicks: 0,
        impressions: 0,
        cpc: 0,
        cpm: 0,
        entries: [],
      });
    }
    const target = groups.get(key);
    target.spend += getMetricValue(entry, 'spend');
    target.gmv += getMetricValue(entry, 'gmv');
    target.orders += getMetricValue(entry, 'orders');
    target.clicks += getMetricValue(entry, 'clicks');
    target.impressions += getMetricValue(entry, 'impressions');
    target.ctr += getMetricValue(entry, 'ctr');
    target.cpc += getMetricValue(entry, 'cpc');
    target.cpm += getMetricValue(entry, 'cpm');
    target.entries.push(entry);
    for (const extra of extraKeys) {
      if (!(extra in target)) {
        target[extra] = 0;
      }
      target[extra] += getMetricValue(entry, extra);
    }
  }
  return Array.from(groups.values());
}

function extractCampaignProductIds(campaignData) {
  const detail = campaignData || {};
  const campaign = detail.campaign || detail;
  const rawDetail = campaign.detail_raw_json || detail.detail_raw_json || {};
  const sources = [
    detail.item_group_id,
    detail.itemGroupId,
    campaign.item_group_id,
    campaign.itemGroupId,
    rawDetail.item_group_id,
    rawDetail.itemGroupId,
    detail.item_group_ids,
    detail.itemGroupIds,
    campaign.item_group_ids,
    campaign.itemGroupIds,
    rawDetail.item_group_ids,
    rawDetail.itemGroupIds,
  ];
  const ids = [];
  for (const source of sources) {
    ensureArray(source).forEach((value) => {
      if (value !== undefined && value !== null && String(value).trim()) {
        ids.push(String(value).trim());
      }
    });
  }
  const sessions = ensureArray(detail.sessions || detail.session_list);
  for (const session of sessions) {
    ensureArray(session?.item_group_ids || session?.itemGroupIds).forEach((value) => {
      if (value !== undefined && value !== null && String(value).trim()) ids.push(String(value).trim());
    });
    ensureArray(session?.product_list || session?.products).forEach((product) => {
      const id = product?.item_group_id || product?.itemGroupId || product?.product_id || product?.spu_id || product?.id;
      if (id !== undefined && id !== null && String(id).trim()) ids.push(String(id).trim());
    });
  }
  return Array.from(new Set(ids));
}

function getProductImage(product) {
  return (
    product?.image_url ||
    product?.product_image_url ||
    product?.cover_image ||
    product?.thumbnail_url ||
    product?.imageUrl ||
    product?.coverImage ||
    product?.main_image ||
    product?.image ||
    null
  );
}

function deriveCampaignMetadata(campaignData, productCatalog = []) {
  if (!campaignData) return {};
  const campaign = campaignData.campaign || campaignData;
  const sessions = ensureArray(campaignData.sessions || campaignData.session_list);
  const productCatalogMap = new Map();
  ensureArray(productCatalog).forEach((product) => {
    const id = getProductIdentifier(product);
    if (id) productCatalogMap.set(String(id), product);
  });
  const products = [];
  for (const session of sessions) {
    const list = ensureArray(session?.product_list || session?.products);
    for (const product of list) {
      const id =
        product.product_id ||
        product.item_id ||
        product.spu_id ||
        product.id ||
        product.item_group_id;
      const catalogProduct = id ? productCatalogMap.get(String(id)) || {} : {};
      products.push({
        id: id ? String(id) : undefined,
        name:
          product.product_name ||
          product.title ||
          product.name ||
          product.item_name ||
          catalogProduct.product_name ||
          catalogProduct.title ||
          catalogProduct.name ||
          catalogProduct.item_name,
        image: getProductImage(product) || getProductImage(catalogProduct),
      });
    }
  }
  for (const id of extractCampaignProductIds(campaignData)) {
    if (products.some((product) => product.id === id)) continue;
    const catalogProduct = productCatalogMap.get(id) || {};
    products.push({
      id,
      name:
        catalogProduct.product_name ||
        catalogProduct.title ||
        catalogProduct.name ||
        catalogProduct.item_name ||
        `商品 ${id}`,
      image: getProductImage(catalogProduct),
    });
  }
  const uniqueProducts = products.filter((item, index, list) => {
    if (!item.id) return index === list.findIndex((entry) => !entry.id);
    return index === list.findIndex((entry) => entry.id === item.id);
  });
  return {
    id: campaign.campaign_id || campaign.id,
    name: campaign.name || campaign.campaign_name || campaign.session_name,
    status:
      campaign.status ||
      campaign.delivery_status ||
      campaign.campaign_status ||
      campaign.operation_status ||
      campaignData.operation_status,
    advertiserName: campaign.advertiser_name || campaign.advertiser_display_name || campaign.advertiser,
    storeName: campaign.store_name || campaign.storeName,
    businessCenterName: campaign.business_center_name || campaign.bc_name,
    shoppingAdsType: campaign.shopping_ads_type,
    optimizationGoal: campaign.optimization_goal,
    storeId: campaign.store_id || campaign.storeId,
    products: uniqueProducts,
    raw: campaignData,
  };
}

function determineStatusLabel(status) {
  if (!status) return '未知';
  const normalized = String(status).toUpperCase();
  if (normalized.includes('PAUSE') || normalized.includes('DISABLE')) return '已暂停';
  if (normalized.includes('ENABLE') || normalized.includes('RUN') || normalized.includes('OK'))
    return '运行中';
  if (normalized.includes('ARCHIVE')) return '已归档';
  return status;
}

function normalizeCreativeStatus(status) {
  if (!status) return 'CANDIDATE';
  const normalized = String(status).toUpperCase();
  if (normalized.includes('NOT_DELIVER')) return 'NOT_DELIVERING';
  if (normalized.includes('NOT_ACTIVE') || normalized.includes('INACTIVE')) return 'NOT_ACTIVE';
  if (normalized.includes('AUTH')) return 'AUTHORIZATION_NEEDED';
  if (normalized.includes('EXCLUD') || normalized.includes('REMOVE')) return 'EXCLUDED';
  if (normalized.includes('REJECT')) return 'REJECTED';
  if (normalized.includes('UNAVAILABLE')) return 'UNAVAILABLE';
  if (normalized.includes('QUEUE')) return 'IN_QUEUE';
  if (normalized.includes('CANDIDATE')) return 'CANDIDATE';
  if (normalized.includes('LEARN')) return 'LEARNING';
  if (normalized.includes('DELIVER')) return 'DELIVERING';
  return 'CANDIDATE';
}

function firstPresent(...values) {
  return values.find((value) => value !== undefined && value !== null && String(value) !== '');
}

function localCreativeMediaUrl(value) {
  const text = String(value || '').trim();
  if (!text) return null;
  if (text.startsWith('/api/v1/')) return text;
  if (typeof window === 'undefined') return null;
  try {
    const parsed = new URL(text, window.location.origin);
    if (parsed.origin === window.location.origin && parsed.pathname.startsWith('/api/v1/')) {
      return `${parsed.pathname}${parsed.search}${parsed.hash}`;
    }
  } catch {
    return null;
  }
  return null;
}

function firstLocalCreativeMedia(...values) {
  for (const value of values) {
    const local = localCreativeMediaUrl(value);
    if (local) return local;
  }
  return null;
}

function extractCreativeThumbnail(source, fallback = null) {
  const videoInfo = source?.video_info || source?.videoInfo || {};
  return (
    firstLocalCreativeMedia(
      source?.local_cover_url,
      source?.localCoverUrl,
      source?.video_cover_url,
      source?.videoCoverUrl,
      source?.cover_url,
      source?.coverUrl,
      source?.thumbnail_url,
      source?.thumbnailUrl,
      source?.thumbnail,
      source?.image,
      videoInfo?.video_cover_url,
      videoInfo?.videoCoverUrl,
      videoInfo?.cover_url,
      videoInfo?.coverUrl,
      fallback,
    )
  );
}

function extractCreativePreviewUrl(source, fallback = null) {
  const videoInfo = source?.video_info || source?.videoInfo || {};
  return (
    firstLocalCreativeMedia(
      source?.local_preview_url,
      source?.localPreviewUrl,
      source?.preview_url,
      source?.previewUrl,
      source?.video_preview_url,
      source?.videoPreviewUrl,
      source?.play_url,
      source?.playUrl,
      videoInfo?.preview_url,
      videoInfo?.previewUrl,
      videoInfo?.play_url,
      videoInfo?.playUrl,
      fallback,
    )
  );
}

function normalizeCreativesData(creativesData, metricsData, heatingData) {
  const rows = new Map();

  const ensureRow = (creativeId, productId = null) => {
    const creativeKey = String(creativeId);
    const productKey = String(productId || '').trim();
    const key =
      creativeKey === '-1' && productKey
        ? `${creativeKey}:${productKey}`
        : creativeKey;
    if (!rows.has(key)) {
      rows.set(key, {
        rowKey: key,
        creativeId: creativeKey,
        name: creativeKey,
        thumbnail: null,
        previewUrl: null,
        videoId: null,
        duration: null,
        status: 'CANDIDATE',
        metrics: parseCreativeMetrics({}),
        historicalMetrics: parseCreativeMetrics({}),
        heating: null,
        metadata: {},
      });
    }
    return rows.get(key);
  };

  for (const item of ensureArray(creativesData?.items ?? creativesData?.list ?? creativesData)) {
    const creativeId =
      item?.item_id ||
      item?.creative_id ||
      item?.creativeId ||
      item?.id ||
      item?.code ||
      item?.creative?.id ||
      item?.creativeCode;
    if (!creativeId) continue;
    const row = ensureRow(
      creativeId,
      firstPresent(
        item?.product_id,
        item?.item_group_id,
        item?.itemGroupId,
      ),
    );
    row.name =
      item?.title ||
      item?.creative_name ||
      item?.creativeName ||
      item?.name ||
      item?.label ||
      item?.title ||
      row.name ||
      creativeId;
    row.thumbnail = extractCreativeThumbnail(item, row.thumbnail);
    row.previewUrl = extractCreativePreviewUrl(item, row.previewUrl);
    row.videoId = firstPresent(item?.video_id, item?.videoId, item?.video_info?.video_id, row.videoId) || null;
    row.duration = firstPresent(item?.duration, item?.video_info?.duration, row.duration) || null;
    const assetStatus = item?.creative_delivery_status || item?.creative_status || item?.status || item?.state;
    row.status = assetStatus ? normalizeCreativeStatus(assetStatus) : 'CANDIDATE';
    row.historicalMetrics = parseCreativeMetrics(item?.metrics || {});
    row.metadata = { ...row.metadata, ...item };
  }

  const metricsItems = metricsData?.items || metricsData?.results;
  if (Array.isArray(metricsItems)) {
    for (const entry of metricsItems) {
      const creativeId =
        entry?.shop_content_id ||
        entry?.item_id ||
        entry?.creative_id ||
        entry?.creativeId ||
        entry?.id ||
        entry?.code ||
        entry?.metrics?.shop_content_id ||
        entry?.metrics?.creative_id ||
        entry?.metrics?.item_id;
      if (!creativeId) continue;
      const row = ensureRow(
        creativeId,
        firstPresent(
          entry?.product_id,
          entry?.item_group_id,
          entry?.itemGroupId,
          entry?.metrics?.product_id,
          entry?.metrics?.item_group_id,
          entry?.metrics?.itemGroupId,
        ),
      );
      row.name =
        entry?.title ||
        entry?.creative_name ||
        entry?.creativeName ||
        entry?.name ||
        entry?.label ||
        entry?.title ||
        row.name ||
        creativeId;
      row.status = normalizeCreativeStatus(
        entry?.creative_delivery_status || entry?.creative_status || entry?.status || entry?.state || row.status,
      );
      row.thumbnail = extractCreativeThumbnail(entry?.metrics || entry, row.thumbnail);
      row.previewUrl = extractCreativePreviewUrl(entry?.metrics || entry, row.previewUrl);
      row.videoId = firstPresent(entry?.video_id, entry?.videoId, entry?.metrics?.video_id, row.videoId) || null;
      row.duration = firstPresent(entry?.duration, entry?.metrics?.duration, row.duration) || null;
      row.metrics = mergeCreativeMetrics(row.metrics, parseCreativeMetrics(entry?.metrics || entry));
      row.metadata = { ...row.metadata, ...entry, ...(entry?.metrics || {}) };
    }
  }

  if (metricsData?.report) {
    for (const entry of ensureArray(metricsData.report?.list)) {
      const dimensions = entry.dimensions || entry.dimension || {};
      const creativeId =
        dimensions.shop_content_id ||
        dimensions.item_id ||
        dimensions.creative ||
        dimensions.creative_id ||
        dimensions.creativeId ||
        dimensions.id ||
        dimensions.code;
      if (!creativeId) continue;
      const row = ensureRow(
        creativeId,
        firstPresent(
          dimensions.product_id,
          dimensions.item_group_id,
          dimensions.itemGroupId,
          entry?.metrics?.product_id,
          entry?.metrics?.item_group_id,
          entry?.metrics?.itemGroupId,
        ),
      );
      row.name =
        entry.metrics?.title ||
        dimensions.title ||
        dimensions.creative_name ||
        dimensions.creativeName ||
        dimensions.name ||
        row.name ||
        creativeId;
      row.status = normalizeCreativeStatus(
        entry.metrics?.creative_delivery_status ||
          dimensions.creative_delivery_status ||
          dimensions.creative_status ||
          dimensions.status ||
          row.status,
      );
      row.thumbnail = extractCreativeThumbnail(entry.metrics || dimensions, row.thumbnail);
      row.previewUrl = extractCreativePreviewUrl(entry.metrics || dimensions, row.previewUrl);
      row.videoId =
        firstPresent(entry.metrics?.video_id, dimensions.video_id, dimensions.videoId, row.videoId) || null;
      row.duration = firstPresent(entry.metrics?.duration, dimensions.duration, row.duration) || null;
      row.metrics = mergeCreativeMetrics(row.metrics, parseCreativeMetrics(entry.metrics || entry));
      const seedFlag =
        entry.is_seed || entry.isSeed || dimensions.is_seed || dimensions.isSeed || entry.metrics?.is_seed || false;
      row.metadata = {
        ...row.metadata,
        ...dimensions,
        ...(entry.metrics || {}),
        is_seed: seedFlag,
      };
    }
  }

  for (const entry of ensureArray(heatingData?.items ?? heatingData?.list ?? heatingData)) {
    const creativeId =
      entry?.creative_id || entry?.creativeId || entry?.id || entry?.code || entry?.creative?.id || entry?.creativeId;
    if (!creativeId) continue;
    const row = ensureRow(
      creativeId,
      firstPresent(
        entry?.product_id,
        entry?.item_group_id,
        entry?.itemGroupId,
      ),
    );
    row.heating = entry;
    row.status = normalizeCreativeStatus(entry?.creative_status || entry?.status || row.status);
    row.metadata = { ...row.metadata, ...entry };
  }

  return Array.from(rows.values());
}

function isCreativeBoosting(creative) {
  const heating = creative?.heating || {};
  if (heating.is_heating_active || heating.isHeatingActive) return true;
  const status = heating.status || heating.state;
  if (status && String(status).toUpperCase() === 'HEATING') return true;
  return Boolean(
    creative?.metadata?.is_boosting || creative?.metadata?.isBoosting || creative?.metadata?.boosting || false,
  );
}

function buildCreativeHeatingIdentityPayload(creative, fallbackItemGroupId) {
  const heating = creative?.heating || {};
  const metadata = creative?.metadata || {};
  const productId =
    heating.product_id ||
    heating.item_group_id ||
    metadata.product_id ||
    metadata.item_group_id ||
    metadata.itemGroupId ||
    fallbackItemGroupId;
  const itemId =
    heating.item_id ||
    metadata.item_id ||
    metadata.itemId ||
    creative?.creativeId ||
    metadata.creative_id;
  return {
    ...(productId ? { product_id: String(productId) } : {}),
    ...(itemId ? { item_id: String(itemId) } : {}),
  };
}

function buildCreativeHeatingPayload(creative, fallbackItemGroupId) {
  const heating = creative?.heating || {};
  const metadata = creative?.metadata || {};
  const spend = Number(creative?.metrics?.spend || creative?.metrics?.cost || 0);
  const rawMode = String(heating.mode || '').trim().toUpperCase();
  const mode = ['STOP', 'STOP_CREATIVE', 'STOP_BOOST'].includes(rawMode)
    ? 'MANUAL'
    : (heating.mode || 'MANUAL');
  const configuredDelta = Number(
    heating.budget_delta ??
    heating.budgetDelta ??
    heating.default_budget_delta ??
    heating.defaultBudgetDelta,
  );
  const configuredTarget = Number(
    heating.target_daily_budget ?? heating.targetDailyBudget,
  );
  const hasPositiveTarget = Number.isFinite(configuredTarget) && configuredTarget > 0;
  const hasPositiveDelta = Number.isFinite(configuredDelta) && configuredDelta > 0;
  return {
    mode,
    ...buildCreativeHeatingIdentityPayload(creative, fallbackItemGroupId),
    ...(heating.currency || metadata.currency
      ? { currency: heating.currency || metadata.currency }
      : {}),
    ...(heating.max_duration_minutes || heating.maxDurationMinutes
      ? {
          max_duration_minutes:
            heating.max_duration_minutes || heating.maxDurationMinutes,
        }
      : {}),
    ...(heating.note ? { note: heating.note } : {}),
    ...(hasPositiveTarget
      ? { target_daily_budget: configuredTarget }
      : {
          budget_delta:
            hasPositiveDelta
              ? configuredDelta
              : Math.max(Number.isFinite(spend) ? spend * 0.1 : 0, 1),
        }),
  };
}

function isSeedCreative(creative) {
  const meta = creative?.metadata || {};
  const heating = creative?.heating || {};
  return Boolean(
    meta.is_seed ||
      meta.isSeed ||
      meta.seed ||
      meta.seed_creative ||
      meta.is_seed_creative ||
      meta.isSeedCreative ||
      heating.is_seed ||
      heating.isSeed,
  );
}

function resolveLastEvaluated(creative) {
  const heating = creative?.heating || {};
  return (
    heating.last_evaluated_at ||
    heating.lastEvaluatedAt ||
    heating.updated_at ||
    heating.updatedAt ||
    creative?.metadata?.updated_at ||
    creative?.metadata?.updatedAt ||
    null
  );
}

function TrendChart({ data, selectedMetrics }) {
  if (!data || data.length === 0) {
    return <div className="gmvmax-chart gmvmax-chart--empty">所选日期范围内暂无趋势数据。</div>;
  }
  const activeOptions = TREND_METRIC_OPTIONS.filter((option) => selectedMetrics.has(option.key));
  const width = 760;
  const height = 260;
  const padding = { top: 18, right: 18, bottom: 34, left: 54 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const maxValue = Math.max(
    ...data.flatMap((point) => activeOptions.map((option) => Number(point[option.key]) || 0)),
    1,
  );
  const xStep = chartWidth / Math.max(data.length - 1, 1);
  const scaleY = (value) => padding.top + chartHeight - ((Number(value) || 0) / maxValue) * chartHeight;
  const buildPath = (key) =>
    data
      .map((point, index) => {
        const x = padding.left + index * xStep;
        const y = scaleY(point[key] || 0);
        return `${index === 0 ? 'M' : 'L'}${x},${y}`;
      })
      .join(' ');
  const xLabels = data.map((point, index) => ({
    x: padding.left + index * xStep,
    label: String(point.label || '').slice(5).replace('-', '/'),
  }));
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    ratio,
    value: maxValue * ratio,
    y: padding.top + chartHeight - chartHeight * ratio,
  }));
  const allMoney = activeOptions.every((option) => option.money);
  const formatAxisValue = (value) => {
    const compact = Number(value).toLocaleString(undefined, {
      maximumFractionDigits: value < 10 ? 1 : 0,
      notation: value >= 1000 ? 'compact' : 'standard',
    });
    return allMoney ? `$${compact}` : compact;
  };
  const formatPointValue = (option, value) => {
    if (option.money) return `$${formatMoney(value)}`;
    if (option.key === 'roas') return formatRoas(value);
    return formatNumber(value, { maximumFractionDigits: 2 });
  };
  return (
    <div className="gmvmax-trend-visual">
      <div className="gmvmax-trend-legend" aria-label="趋势图图例">
        {activeOptions.map((option) => (
          <span key={option.key}>
            <i style={{ backgroundColor: option.color }} />
            {option.label}
          </span>
        ))}
      </div>
      <svg className="gmvmax-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="投放趋势图">
        <title>投放趋势图</title>
        <g className="gmvmax-chart__grid">
          {yTicks.map((tick) => (
            <Fragment key={tick.ratio}>
              <line x1={padding.left} y1={tick.y} x2={width - padding.right} y2={tick.y} />
              <text x={padding.left - 8} y={tick.y + 4} textAnchor="end">
                {formatAxisValue(tick.value)}
              </text>
            </Fragment>
          ))}
        </g>
        {activeOptions.map((option) => (
          <g key={option.key}>
            <path d={buildPath(option.key)} className="gmvmax-chart__line" style={{ stroke: option.color }} />
            {data.map((point, index) => (
              <circle
                key={`${option.key}-${point.label}`}
                cx={padding.left + index * xStep}
                cy={scaleY(point[option.key])}
                r="3.5"
                fill={option.color}
                className="gmvmax-chart__point"
              >
                <title>{`${formatChineseDate(point.label)} · ${option.label} ${formatPointValue(option, point[option.key])}`}</title>
              </circle>
            ))}
          </g>
        ))}
        {xLabels.map((item) => (
          <text key={item.x} x={item.x} y={height - 8} textAnchor="middle" className="gmvmax-chart__label">
            {item.label}
          </text>
        ))}
      </svg>
    </div>
  );
}

function StrategyRuleEditor({ rule, onChange, onRemove }) {
  const handleChange = (field) => (event) => {
    onChange({ ...rule, [field]: event.target.value });
  };

  return (
    <div className="gmvmax-rule">
      <div className="gmvmax-rule__row">
        <FormField label="指标">
          <select value={rule.metric} onChange={handleChange('metric')}>
            {METRIC_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="运算符">
          <select value={rule.operator} onChange={handleChange('operator')}>
            {OPERATOR_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="阈值">
          <input type="number" value={rule.value} onChange={handleChange('value')} />
        </FormField>
      </div>
      <div className="gmvmax-rule__row">
        <FormField label="条件关系">
          <select value={rule.conjunction} onChange={handleChange('conjunction')}>
            <option value="AND">且 (AND)</option>
            <option value="OR">或 (OR)</option>
          </select>
        </FormField>
        <FormField label="副指标（可选）">
          <select value={rule.secondaryMetric} onChange={handleChange('secondaryMetric')}>
            <option value="">—</option>
            {METRIC_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="运算符">
          <select value={rule.secondaryOperator} onChange={handleChange('secondaryOperator')}>
            {OPERATOR_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="阈值">
          <input type="number" value={rule.secondaryValue} onChange={handleChange('secondaryValue')} />
        </FormField>
      </div>
      <div className="gmvmax-rule__row">
        <FormField label="操作">
          <select value={rule.action} onChange={handleChange('action')}>
            {ACTION_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="操作值">
          <input type="number" value={rule.actionValue} onChange={handleChange('actionValue')} />
        </FormField>
        <button type="button" className="gmvmax-rule__remove" onClick={onRemove}>
          删除规则
        </button>
      </div>
    </div>
  );
}

function BudgetDialog({ open, mode, onClose, onSubmit }) {
  const [value, setValue] = useState('10');

  useEffect(() => {
    if (open) {
      setValue('10');
    }
  }, [open]);

  const handleSubmit = useCallback(
    (event) => {
      event.preventDefault();
      const percent = Number(value);
      if (!Number.isFinite(percent) || percent <= 0) return;
      onSubmit(percent);
    },
    [onSubmit, value],
  );

  const title = mode === 'increase' ? '提升预算' : '降低预算';

  return (
    <Modal open={open} title={title} onClose={onClose}>
      <form className="gmvmax-budget-dialog" onSubmit={handleSubmit}>
        <FormField label="调整幅度 (%)">
          <input
            type="number"
            min="1"
            step="1"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </FormField>
        <div className="gmvmax-budget-dialog__actions">
          <button type="button" onClick={onClose}>
            {GmvMaxTexts.cancel}
          </button>
          <button type="submit" className="primary">
            确认调整
          </button>
        </div>
      </form>
    </Modal>
  );
}

export default function GmvMaxCampaignDetailPage() {
  const { wid: workspaceId, campaignId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const provider = searchParams.get('provider') || '';
  const authId = searchParams.get('authId') || '';
  const advertiserId = searchParams.get('advertiserId') || '';
  const storeIdFromQuery = searchParams.get('storeId') || '';
  const advertiserTimezoneFromQuery = searchParams.get('timezone') || '';

  const resolveTab = useCallback((params) => {
    const value = params.get('tab');
    if (value === 'dashboard' || value === 'products') return value;
    return 'automation';
  }, []);

  const [activeTab, setActiveTab] = useState(() => resolveTab(searchParams));
  const [timeRange, setTimeRange] = useState(searchParams.get('range') || '7d');
  const [customRange, setCustomRange] = useState({
    start: searchParams.get('start_date') || '',
    end: searchParams.get('end_date') || '',
  });
  const [actionLogsOpen, setActionLogsOpen] = useState(false);
  const [budgetDialog, setBudgetDialog] = useState({ open: false, mode: 'increase' });
  const [strategyDraft, setStrategyDraft] = useState(() => normalizeStrategyResponse(null));
  const [strategyDirty, setStrategyDirty] = useState(false);
  const [lastSaveMessage, setLastSaveMessage] = useState('');
  const [showAdvancedRules, setShowAdvancedRules] = useState(false);
  const [creativeStatusFilters, setCreativeStatusFilters] = useState(
    () => new Set(ALL_CREATIVE_STATUS_KEYS),
  );
  const [showOnlyHeated, setShowOnlyHeated] = useState(false);
  const [showSeedOnly, setShowSeedOnly] = useState(false);
  const [creativeSortKey, setCreativeSortKey] = useState('spend');
  const [creativeSortDirection, setCreativeSortDirection] = useState('desc');
  const [trendMetrics, setTrendMetrics] = useState(() => new Set(['spend', 'gmv']));
  const [productSelection, setProductSelection] = useState(() => new Set());
  const [productSearch, setProductSearch] = useState('');
  const [productMessage, setProductMessage] = useState('');
  const queryClient = useQueryClient();

  useEffect(() => {
    setActiveTab(resolveTab(searchParams));
  }, [resolveTab, searchParams]);

  const commonEnabled = Boolean(workspaceId && provider && authId && campaignId);

  const accountsQuery = useAccountsQuery(
    workspaceId,
    provider,
    { page_size: 100 },
    { enabled: Boolean(workspaceId && provider), staleTime: 5 * 60 * 1000 },
  );
  const advertisersQuery = useAdvertisersQuery(
    workspaceId,
    provider,
    authId,
    {},
    { enabled: commonEnabled, staleTime: 5 * 60 * 1000 },
  );
  const storesQuery = useStoresQuery(
    workspaceId,
    provider,
    authId,
    { advertiserId: advertiserId || undefined },
    { enabled: commonEnabled, staleTime: 5 * 60 * 1000 },
  );
  const advertiserTimezoneFromMetadata = useMemo(() => {
    const advertisers = ensureArray(
      advertisersQuery.data?.items ??
        advertisersQuery.data?.list ??
        advertisersQuery.data,
    );
    const selected = advertisers.find((item) => {
      const id = item?.advertiser_id || item?.advertiserId || item?.id;
      return advertiserId && String(id || '') === String(advertiserId);
    });
    return (
      selected?.display_timezone ||
      selected?.displayTimezone ||
      selected?.timezone ||
      selected?.time_zone ||
      selected?.timeZone ||
      ''
    );
  }, [advertiserId, advertisersQuery.data]);
  const advertiserTimezoneSource =
    advertiserTimezoneFromQuery || advertiserTimezoneFromMetadata;
  const advertiserTimezone = useMemo(
    () => resolveTimezoneLabel(advertiserTimezoneSource),
    [advertiserTimezoneSource],
  );
  const hasAdvertiserTimezone = Boolean(advertiserTimezoneSource);

  const metricsParams = useMemo(
    () => computeTimeRange(timeRange, customRange, advertiserTimezone),
    [advertiserTimezone, customRange, timeRange],
  );

  const syncReportParams = useMemo(
    () => ({
      ...metricsParams,
      metrics: DEFAULT_REPORT_METRICS,
      dimensions: ['campaign_id', 'stat_time_day'],
      enable_total_metrics: true,
      store_ids: storeIdFromQuery ? [String(storeIdFromQuery)] : undefined,
    }),
    [metricsParams, storeIdFromQuery],
  );

  const campaignQuery = useGmvMaxCampaignQuery(workspaceId, provider, authId, campaignId, {
    enabled: commonEnabled,
  });

  const campaignFilterId = useMemo(() => {
    const campaign = campaignQuery.data?.campaign || campaignQuery.data;
    const resolvedId = campaign?.campaign_id || campaign?.campaignId || campaign?.id || campaignId;
    return resolvedId ? String(resolvedId) : '';
  }, [campaignId, campaignQuery.data]);

  const campaignStoreId = useMemo(() => {
    const campaign = campaignQuery.data?.campaign || campaignQuery.data;
    return campaign?.store_id || campaign?.storeId || storeIdFromQuery || '';
  }, [campaignQuery.data, storeIdFromQuery]);

  const itemGroupIds = useMemo(
    () => extractCampaignProductIds(campaignQuery.data),
    [campaignQuery.data],
  );
  // Heating actions remain single-creative/single-product operations. The
  // creative report and manual sync use the complete campaign binding below.
  const itemGroupId = itemGroupIds[0] || '';

  const productFiltersReady = Boolean(campaignFilterId);
  const creativeFiltersReady = Boolean(
    campaignQuery.isSuccess && campaignFilterId && campaignStoreId && itemGroupIds.length > 0,
  );

  const campaignIdsForSync = useMemo(() => {
    const currentCampaignId = String(campaignFilterId || campaignId || '').trim();
    return currentCampaignId ? [currentCampaignId] : [];
  }, [campaignFilterId, campaignId]);

  const { ensureFresh, isSyncing: isEnsuringFresh } = useEnsureFreshGmvData({
    workspaceId,
    provider,
    authId,
    campaignId: campaignFilterId || campaignId,
    reportParams: syncReportParams,
    levels: ['CAMPAIGN', 'PRODUCT', 'CREATIVE'],
    campaignIds: campaignIdsForSync,
    itemGroupIds: itemGroupIds.length > 0 ? itemGroupIds : undefined,
  });

  const isDashboardTab = activeTab === 'dashboard';
  const isProductsTab = activeTab === 'products';
  const creativeStatusOptions = useMemo(
    () => [
      { key: 'DELIVERING', label: GmvMaxTexts.creativeStatusDelivering },
      { key: 'LEARNING', label: GmvMaxTexts.creativeStatusLearning },
      { key: 'IN_QUEUE', label: GmvMaxTexts.creativeStatusInQueue },
      { key: 'AUTHORIZATION_NEEDED', label: GmvMaxTexts.creativeStatusAuthorizationNeeded },
      { key: 'NOT_ACTIVE', label: GmvMaxTexts.creativeStatusNotActive },
      { key: 'NOT_DELIVERING', label: GmvMaxTexts.creativeStatusNotDelivering },
      { key: 'EXCLUDED', label: GmvMaxTexts.creativeStatusExcluded },
      { key: 'REJECTED', label: GmvMaxTexts.creativeStatusRejected },
      { key: 'UNAVAILABLE', label: GmvMaxTexts.creativeStatusUnavailable },
      { key: 'CANDIDATE', label: '候选素材' },
    ],
    [],
  );

  const {
    campaignMetrics: campaignMetricsQuery,
    productMetrics: productMetricsQuery,
    creativeMetrics: creativeMetricsQuery,
  } = useGmvMaxMetrics({
    workspaceId,
    provider,
    authId,
    campaignId,
    advertiserId,
    metricsParams,
    campaignFilterId,
    itemGroupId,
    itemGroupIds,
    enabled: commonEnabled && productFiltersReady,
    creativeEnabled: commonEnabled && isDashboardTab && creativeFiltersReady,
  });
  const campaignFreshness = useMemo(
    () => formatDataFreshness(campaignMetricsQuery.data?.freshness),
    [campaignMetricsQuery.data?.freshness],
  );
  const creativeFreshness = useMemo(
    () => formatDataFreshness(creativeMetricsQuery.data?.freshness),
    [creativeMetricsQuery.data?.freshness],
  );

  const creativesQuery = useGmvMaxCreativeAssetsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    {
      store_id: campaignStoreId || undefined,
      advertiser_id: advertiserId || undefined,
      item_group_ids: itemGroupIds.length > 0 ? itemGroupIds : undefined,
      lookback_days: 30,
      page_size: 100,
      fetch_all_pages: true,
      refresh: false,
    },
    {
      enabled: commonEnabled && isDashboardTab && creativeFiltersReady,
      refetchInterval: 60 * 1000,
    },
  );

  const creativeHeatingQuery = useGmvMaxCreativeHeatingQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    undefined,
    {
      enabled: commonEnabled && isDashboardTab,
    },
  );

  const sessionList = useMemo(
    () => ensureArray(campaignQuery.data?.sessions || campaignQuery.data?.session_list),
    [campaignQuery.data],
  );
  const campaignStoreBcId = useMemo(() => {
    const campaign = campaignQuery.data?.campaign || campaignQuery.data || {};
    const store = campaign?.store || {};
    return (
      campaign?.store_authorized_bc_id ||
      campaign?.authorized_bc_id ||
      campaign?.bc_id ||
      store?.store_authorized_bc_id ||
      store?.bc_id ||
      undefined
    );
  }, [campaignQuery.data]);
  const sessionProducts = useMemo(() => {
    const products = [];
    sessionList.forEach((session) => {
      ensureArray(session?.product_list || session?.products).forEach((product) => {
        if (product) products.push(product);
      });
    });
    return products;
  }, [sessionList]);
  const initialProductSet = useMemo(() => {
    const set = new Set();
    extractCampaignProductIds(campaignQuery.data).forEach((id) => {
      if (id) set.add(String(id));
    });
    sessionProducts.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) set.add(String(id));
    });
    return set;
  }, [campaignQuery.data, sessionProducts]);

  useEffect(() => {
    setProductSelection(new Set(initialProductSet));
  }, [initialProductSet]);

  const productsQuery = useProductsQuery(
    workspaceId,
    provider,
    authId,
    {
      store_id: storeIdFromQuery || campaignStoreId || undefined,
      advertiser_id: advertiserId || undefined,
      owner_bc_id: campaignStoreBcId,
      page_size: 500,
    },
    {
      enabled: Boolean(commonEnabled && campaignQuery.isSuccess && (storeIdFromQuery || campaignStoreId)),
      staleTime: 30 * 1000,
      refetchInterval: 60 * 1000,
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
    },
  );

  const mergedProducts = useMemo(() => {
    const map = new Map();
    const queried = Array.isArray(productsQuery.data?.items)
      ? productsQuery.data.items
      : Array.isArray(productsQuery.data?.list)
        ? productsQuery.data.list
        : Array.isArray(productsQuery.data)
          ? productsQuery.data
          : [];
    queried.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) map.set(id, product);
    });
    sessionProducts.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id && !map.has(id)) {
        map.set(id, product);
      }
    });
    return Array.from(map.values());
  }, [productsQuery.data, sessionProducts]);

  const storeNameMap = useMemo(() => {
    const map = new Map();
    if (campaignStoreId) {
      const storeLabel = getStoreLabel(campaignQuery.data?.campaign?.store || {}) || String(campaignStoreId);
      map.set(String(campaignStoreId), storeLabel);
    }
    if (storeIdFromQuery && !map.has(storeIdFromQuery)) {
      map.set(storeIdFromQuery, storeIdFromQuery);
    }
    return map;
  }, [campaignQuery.data, campaignStoreId, storeIdFromQuery]);

  const strategyQuery = useGmvMaxStrategyQuery(workspaceId, provider, authId, campaignId, {
    enabled: commonEnabled,
  });

  const notifications = useGmvMaxNotifications({
    workspaceId,
    provider,
    authId,
    campaignId,
    enabled: commonEnabled,
  });

  const metricsSync = useGmvMaxMetricsSync({ workspaceId, provider, authId, campaignId });
  const lastMetricsSyncIdRef = useRef(null);

  useEffect(() => {
    const taskId = metricsSync.task?.task_id || metricsSync.task?.taskId;
    const state = normalizeTaskState(metricsSync.task?.state);
    if (!taskId || state !== 'SUCCESS') return;
    if (lastMetricsSyncIdRef.current === taskId) return;
    lastMetricsSyncIdRef.current = taskId;

    const metricsQueryKey = composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId);
    queryClient.invalidateQueries({ queryKey: metricsQueryKey, exact: false });
    queryClient.refetchQueries({ queryKey: metricsQueryKey, exact: false, type: 'all' });
  }, [
    authId,
    campaignId,
    metricsSync.task?.state,
    metricsSync.task?.taskId,
    metricsSync.task?.task_id,
    provider,
    queryClient,
    workspaceId,
  ]);
  const applyActionMutation = useApplyGmvMaxActionMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      campaignQuery.refetch();
      queryClient.invalidateQueries({
        queryKey: composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId),
        exact: false,
      });
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'action-logs'] });
    },
  });
  const creativeActionSuccess = useCallback(() => {
    queryClient.invalidateQueries({
      queryKey: composeMetricsQueryBaseKey(workspaceId, provider, authId, campaignId),
      exact: false,
    });
    queryClient.invalidateQueries({ queryKey: ['gmvMax', 'creative-heating'] });
  }, [authId, campaignId, provider, queryClient, workspaceId]);
  const startCreativeHeatingMutation = useStartGmvMaxCreativeHeatingMutation(
    workspaceId,
    provider,
    authId,
    campaignId,
    {
      onSuccess: creativeActionSuccess,
    },
  );
  const stopCreativeHeatingMutation = useStopGmvMaxCreativeHeatingMutation(
    workspaceId,
    provider,
    authId,
    campaignId,
    {
      onSuccess: creativeActionSuccess,
    },
  );
  const creativeActionPending =
    startCreativeHeatingMutation.isPending || stopCreativeHeatingMutation.isPending;
  const creativeActionError =
    startCreativeHeatingMutation.error || stopCreativeHeatingMutation.error;
  const updateProductsMutation = useUpdateGmvMaxStrategyMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      setProductMessage('商品已更新');
      campaignQuery.refetch();
    },
    onError: (error) => setProductMessage(error?.message || '商品更新失败'),
  });
  const updateStrategyMutation = useUpdateGmvMaxStrategyMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      setStrategyDirty(false);
      setLastSaveMessage('保存成功');
      strategyQuery.refetch();
    },
    onError: (error) => {
      setLastSaveMessage(error?.message || '策略保存失败。');
    },
  });
  const previewStrategyMutation = usePreviewGmvMaxStrategyMutation(workspaceId, provider, authId, campaignId, {
    onError: () => {},
  });

  useEffect(() => {
    if (strategyQuery.data && !strategyDirty) {
      setStrategyDraft(normalizeStrategyResponse(strategyQuery.data));
    }
  }, [strategyDirty, strategyQuery.data]);

  const campaignMetadata = useMemo(
    () => deriveCampaignMetadata(campaignQuery.data, mergedProducts),
    [campaignQuery.data, mergedProducts],
  );
  const isDeleted = useMemo(
    () => isCampaignDeleted(campaignQuery.data?.campaign || campaignQuery.data),
    [campaignQuery.data],
  );
  const isReadOnly = isDeleted;

  const accountDisplayName = useMemo(() => {
    const account = extractResourceItems(accountsQuery.data).find(
      (item) => String(item?.id ?? item?.auth_id ?? item?.authId ?? '') === String(authId),
    );
    return (
      readableResourceName(
        account?.label ||
          account?.account_name ||
          account?.accountName ||
          account?.alias ||
          account?.display_name ||
          account?.displayName ||
          account?.name,
        authId,
      ) || '名称待同步'
    );
  }, [accountsQuery.data, authId]);
  const advertiserDisplayName = useMemo(() => {
    const advertiser = extractResourceItems(advertisersQuery.data).find(
      (item) =>
        String(item?.advertiser_id ?? item?.advertiserId ?? item?.id ?? '') === String(advertiserId),
    );
    return (
      readableResourceName(
        campaignMetadata.advertiserName ||
          advertiser?.display_name ||
          advertiser?.displayName ||
          advertiser?.name ||
          advertiser?.advertiser_name,
        advertiserId,
      ) || '名称待同步'
    );
  }, [advertiserId, advertisersQuery.data, campaignMetadata.advertiserName]);
  const storeDisplayName = useMemo(() => {
    const storeId = campaignStoreId || storeIdFromQuery;
    const store = extractResourceItems(storesQuery.data).find(
      (item) => String(item?.store_id ?? item?.storeId ?? item?.id ?? '') === String(storeId),
    );
    return (
      readableResourceName(
        campaignMetadata.storeName || store?.store_name || store?.storeName || store?.name,
        storeId,
      ) || '名称待同步'
    );
  }, [campaignMetadata.storeName, campaignStoreId, storeIdFromQuery, storesQuery.data]);

  const metricsSummary = useMemo(
    () => summarizeMetrics(campaignMetricsQuery.data?.report),
    [campaignMetricsQuery.data],
  );
  const trendSeries = useMemo(
    () => buildTrendSeries(campaignMetricsQuery.data?.report, metricsParams),
    [campaignMetricsQuery.data, metricsParams],
  );
  const productTable = useMemo(
    () => {
      const rows = buildDimensionTable(productMetricsQuery.data?.report, 'product_id');
      const boundProducts = ensureArray(campaignMetadata.products);
      return rows.map((row) => {
        const isUnknown = !row.id || String(row.id).toLowerCase() === 'unknown';
        const matched =
          (!isUnknown && boundProducts.find((product) => String(product.id) === String(row.id))) ||
          (isUnknown && boundProducts.length === 1 ? boundProducts[0] : null);
        if (!matched) return row;
        return {
          ...row,
          id: matched.id || row.id,
          name: matched.name || row.name,
          image: matched.image || row.image,
        };
      });
    },
    [campaignMetadata.products, productMetricsQuery.data],
  );
  const creatives = useMemo(
    () =>
      normalizeCreativesData(creativesQuery.data, creativeMetricsQuery.data, creativeHeatingQuery.data).sort(
        (a, b) => (b.metrics?.gmv || 0) - (a.metrics?.gmv || 0),
      ),
    [creativeHeatingQuery.data, creativeMetricsQuery.data, creativesQuery.data],
  );
  const creativeStatusCounts = useMemo(() => {
    const counts = new Map();
    creatives.forEach((creative) => {
      const key = creative.status || 'CANDIDATE';
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    return counts;
  }, [creatives]);
  const seedCreatives = useMemo(
    () =>
      creatives
        .filter((creative) => isSeedCreative(creative))
        .sort((a, b) => (b.metrics?.roas || 0) - (a.metrics?.roas || 0)),
    [creatives],
  );
  const filteredCreatives = useMemo(() => {
    return creatives.filter((creative) => {
      if (!creativeStatusFilters.has(creative.status || 'CANDIDATE')) return false;
      if (showOnlyHeated && !isCreativeBoosting(creative)) return false;
      if (showSeedOnly && !isSeedCreative(creative)) return false;
      return true;
    });
  }, [creativeStatusFilters, creatives, showOnlyHeated, showSeedOnly]);

  const sortedCreatives = useMemo(() => {
    const list = [...filteredCreatives];
    const getValue = (creative) => {
      const metrics = creative.metrics || {};
      return Number(metrics[creativeSortKey]) || 0;
    };
    const multiplier = creativeSortDirection === 'asc' ? 1 : -1;
    return list.sort((a, b) => (getValue(a) - getValue(b)) * multiplier);
  }, [creativeSortDirection, creativeSortKey, filteredCreatives]);

  const creativeMetricsTotals = useMemo(
    () => summarizeCreativeMetrics(sortedCreatives),
    [sortedCreatives],
  );

  const creativesByStatus = useMemo(() => {
    const groups = new Map();
    sortedCreatives.forEach((creative) => {
      const key = creative.status || 'CANDIDATE';
      if (!groups.has(key)) {
        groups.set(key, []);
      }
      groups.get(key).push(creative);
    });
    const visibleStatuses = creativeStatusOptions.filter((option) => creativeStatusFilters.has(option.key));
    return visibleStatuses
      .map((option) => ({
        key: option.key,
        label: option.label,
        items: groups.get(option.key) || [],
      }))
      .filter((group) => group.items.length > 0);
  }, [creativeStatusFilters, creativeStatusOptions, sortedCreatives]);

  const creativeStatusLabelMap = useMemo(
    () => new Map(creativeStatusOptions.map((item) => [item.key, item.label])),
    [creativeStatusOptions],
  );
  const creativesLoading =
    creativesQuery.isLoading || creativeMetricsQuery.isLoading || creativeHeatingQuery.isLoading;
  const creativesError = creativesQuery.error || creativeMetricsQuery.error || creativeHeatingQuery.error;
  const creativesErrorMessage = resolveMetricsError(creativesError);
  const summaryError = campaignMetricsQuery.error || productMetricsQuery.error;
  const summaryErrorMessage = resolveMetricsError(summaryError);

  const productsChanged = useMemo(() => {
    if (productSelection.size !== initialProductSet.size) return true;
    for (const id of productSelection) {
      if (!initialProductSet.has(id)) return true;
    }
    return false;
  }, [initialProductSet, productSelection]);

  const sessionId = useMemo(() => {
    const session = sessionList[0];
    return session?.session_id || session?.sessionId || session?.id || '';
  }, [sessionList]);

  const identityList = useMemo(() => {
    const identities = ensureArray(sessionList[0]?.identities || sessionList[0]?.identity_list);
    return identities
      .map((identity) => (typeof identity === 'object' ? identity.identity_id || identity.id : identity))
      .filter(Boolean)
      .map(String);
  }, [sessionList]);

  const toggleProduct = useCallback(
    (id) => {
      setProductSelection((prev) => {
        const next = new Set(prev);
        if (next.has(id)) {
          next.delete(id);
        } else {
          next.add(id);
        }
        return next;
      });
    },
    [setProductSelection],
  );

  const toggleAllProducts = useCallback((ids) => {
    setProductSelection((prev) => {
      const next = new Set(prev);
      const shouldSelectAll = ids.some((id) => !next.has(id));
      ids.forEach((id) => {
        if (shouldSelectAll) {
          next.add(id);
        } else {
          next.delete(id);
        }
      });
      return next;
    });
  }, []);

  const selectAllProducts = useCallback((ids) => setProductSelection(new Set(ids)), []);
  const clearProducts = useCallback(() => setProductSelection(new Set()), []);

  const handleSaveProducts = useCallback(async () => {
    if (isReadOnly) return;
    if (!sessionId) {
      setProductMessage('无法更新商品：缺少 session 信息');
      return;
    }
    const payload = {
      session: {
        session_id: sessionId,
        store_id: storeIdFromQuery || campaignStoreId || undefined,
        product_list: Array.from(productSelection).map((id) => ({ spu_id: String(id) })),
        identities: identityList.length > 0 ? identityList : undefined,
      },
    };
    setProductMessage('');
    await updateProductsMutation.mutateAsync(payload);
  }, [campaignStoreId, identityList, isReadOnly, productSelection, sessionId, storeIdFromQuery, updateProductsMutation]);

  const handleTabChange = useCallback(
    (tab) => {
      setActiveTab(tab);
      const next = new URLSearchParams(searchParams);
      next.set('tab', tab);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const handleTimeRangeChange = useCallback(
    (range) => {
      setTimeRange(range);
      const next = new URLSearchParams(searchParams);
      next.set('range', range);
      if (range !== 'custom') {
        next.delete('start_date');
        next.delete('end_date');
      }
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const handleCustomRangeChange = useCallback(
    (key, value) => {
      setCustomRange((prev) => ({ ...prev, [key]: value }));
    },
    [],
  );

  useEffect(() => {
    if (timeRange === 'custom' && customRange.start && customRange.end) {
      const next = new URLSearchParams(searchParams);
      next.set('start_date', customRange.start);
      next.set('end_date', customRange.end);
      setSearchParams(next, { replace: true });
    }
  }, [customRange.end, customRange.start, searchParams, setSearchParams, timeRange]);

  const handleSyncMetrics = useCallback(async () => {
    if (isReadOnly || metricsSync.isSyncing) return;
    try {
      await metricsSync.startSyncAsync({
        start_date: metricsParams.start_date,
        end_date: metricsParams.end_date,
        campaign_ids: campaignIdsForSync,
        item_group_ids: itemGroupIds.length > 0 ? itemGroupIds : undefined,
        levels: ['CAMPAIGN', 'PRODUCT', 'CREATIVE'],
      });
    } catch (error) {
      // eslint-disable-next-line no-console
      console.error('Failed to sync GMV Max metrics', error);
    }
  }, [
    campaignIdsForSync,
    isReadOnly,
    metricsSync,
    itemGroupIds,
    metricsParams.end_date,
    metricsParams.start_date,
  ]);

  useEffect(() => {
    if (metricsSync.syncError) {
      message.error(metricsSync.syncError.message || 'GMV Max 数据同步失败，请稍后重试。');
    }
  }, [metricsSync.syncError]);

  const handleToggleCreativeStatus = useCallback((status) => {
    setCreativeStatusFilters((prev) => {
      const next = new Set(prev);
      if (next.has(status)) {
        next.delete(status);
      } else {
        next.add(status);
      }
      return next.size === 0 ? prev : next;
    });
  }, []);

  const handleToggleTrendMetric = useCallback((metric) => {
    setTrendMetrics((prev) => {
      const next = new Set(prev);
      if (next.has(metric)) {
        if (next.size === 1) return prev;
        next.delete(metric);
      } else {
        next.add(metric);
      }
      return next;
    });
  }, []);

  const handleBoostCreative = useCallback(
    async (creative) => {
      const creativeId = creative?.creativeId || creative?.metadata?.id;
      if (!creativeId || creativeActionPending) return;
      if (isReadOnly) return;
      const fresh = await ensureFresh();
      if (!fresh) {
        message.warning('实时数据尚未同步完成，本次加热未执行，请稍后重试。');
        return;
      }
      startCreativeHeatingMutation.mutate({
        creativeId,
        payload: buildCreativeHeatingPayload(creative, itemGroupId),
      });
    },
    [creativeActionPending, ensureFresh, isReadOnly, itemGroupId, startCreativeHeatingMutation],
  );

  const handleStopHeat = useCallback(
    async (creative) => {
      const creativeId = creative?.creativeId || creative?.metadata?.id;
      if (!creativeId || creativeActionPending) return;
      if (isReadOnly) return;
      const fresh = await ensureFresh();
      if (!fresh) {
        message.warning('实时数据尚未同步完成，本次停止操作未执行，请稍后重试。');
        return;
      }
      stopCreativeHeatingMutation.mutate({
        creativeId,
        payload: buildCreativeHeatingIdentityPayload(creative, itemGroupId),
      });
    },
    [creativeActionPending, ensureFresh, isReadOnly, itemGroupId, stopCreativeHeatingMutation],
  );

  const handlePause = useCallback(async () => {
    if (isReadOnly) return;
    try {
      const result = await applyActionMutation.mutateAsync({ type: 'pause' });
      if (result?.status === 'queued') {
        message.info('暂停请求已接管，正以最高优先级执行；仅等待当前同账户请求结束。');
      } else {
        message.success('系列已暂停。');
      }
    } catch (error) {
      message.error(resolveActionError(error, '暂停系列失败，请稍后重试。'));
    }
  }, [applyActionMutation, isReadOnly]);

  const handleResume = useCallback(async () => {
    if (isReadOnly) return;
    try {
      await applyActionMutation.mutateAsync({ type: 'resume' });
      message.success('系列已启用。');
    } catch (error) {
      message.error(resolveActionError(error, '启用系列失败，请稍后重试。'));
    }
  }, [applyActionMutation, isReadOnly]);

  const handleDelete = useCallback(async () => {
    if (isReadOnly) return;
    const confirmed = window.confirm('确定删除该系列？此操作无法恢复。');
    if (!confirmed) return;
    try {
      await applyActionMutation.mutateAsync({ type: 'delete' });
      message.success('系列已删除。');
    } catch (error) {
      message.error(resolveActionError(error, '删除系列失败，请稍后重试。'));
    }
  }, [applyActionMutation, isReadOnly]);

  const openBudgetDialog = useCallback((mode) => {
    setBudgetDialog({ open: true, mode });
  }, []);

  const closeBudgetDialog = useCallback(() => {
    setBudgetDialog((prev) => ({ ...prev, open: false }));
  }, []);

  const handleBudgetSubmit = useCallback(
    async (percent) => {
      if (isReadOnly) return;
      const payload = {
        type: 'update_budget',
        payload: {
          direction: budgetDialog.mode === 'increase' ? 'increase' : 'decrease',
          percent,
        },
      };
      const fresh = await ensureFresh();
      if (!fresh) {
        message.warning('实时数据尚未同步完成，预算未修改，请稍后重试。');
        return;
      }
      try {
        await applyActionMutation.mutateAsync(payload);
        closeBudgetDialog();
      } catch (error) {
        message.error(resolveActionError(error, '预算调整失败，请稍后重试。'));
      }
    },
    [applyActionMutation, budgetDialog.mode, closeBudgetDialog, ensureFresh, isReadOnly],
  );

  const openActionLogs = useCallback(() => {
    setActionLogsOpen(true);
  }, []);
  const closeActionLogs = useCallback(() => {
    setActionLogsOpen(false);
  }, []);

  const handleRuleChange = useCallback((index, nextRule) => {
    setStrategyDraft((prev) => {
      const nextRules = [...(prev.rules || [])];
      nextRules[index] = nextRule;
      return { ...prev, rules: nextRules };
    });
    setStrategyDirty(true);
  }, []);

  const handleAddRule = useCallback(() => {
    setStrategyDraft((prev) => ({ ...prev, rules: [...(prev.rules || []), createEmptyRule()] }));
    setStrategyDirty(true);
  }, []);

  const handleRemoveRule = useCallback((index) => {
    setStrategyDraft((prev) => {
      const nextRules = [...(prev.rules || [])];
      nextRules.splice(index, 1);
      return { ...prev, rules: nextRules };
    });
    setStrategyDirty(true);
  }, []);

  const handleStrategyFieldChange = useCallback((field, value) => {
    setStrategyDraft((prev) => ({ ...prev, [field]: value }));
    setStrategyDirty(true);
  }, []);

  const handleThresholdChange = useCallback((field, value) => {
    setStrategyDraft((prev) => ({
      ...prev,
      thresholds: {
        ...(prev.thresholds || {}),
        [field]: value,
      },
    }));
    setStrategyDirty(true);
  }, []);

  const handleStrategyReset = useCallback(() => {
    if (strategyQuery.data) {
      setStrategyDraft(normalizeStrategyResponse(strategyQuery.data));
      setStrategyDirty(false);
    }
  }, [strategyQuery.data]);

  const handleStrategySave = useCallback(() => {
    if (isReadOnly) return;
    setLastSaveMessage('');
    updateStrategyMutation.mutate(buildStrategyPayload(strategyDraft));
  }, [isReadOnly, strategyDraft, updateStrategyMutation]);

  const handleStrategyPreview = useCallback(() => {
    if (isReadOnly) return;
    const payload = {
      store_id: storeIdFromQuery || campaignMetadata.storeId,
      shopping_ads_type: campaignMetadata.shoppingAdsType,
      optimization_goal: campaignMetadata.optimizationGoal,
      item_group_ids: ensureArray(campaignMetadata.raw?.sessions)
        .flatMap((session) => ensureArray(session?.product_list))
        .map((product) => product?.item_group_id || product?.product_id)
        .filter(Boolean),
      automation: buildStrategyPayload(strategyDraft),
    };
    previewStrategyMutation.mutate(payload);
  }, [campaignMetadata, isReadOnly, previewStrategyMutation, storeIdFromQuery, strategyDraft]);

  const latestPreviewResult = previewStrategyMutation.data;

  const remoteStatusLabel = determineStatusLabel(campaignMetadata.status);
  const isCampaignRunning = remoteStatusLabel === '运行中';
  const automationEnabled = Boolean(
    strategyQuery.data && normalizeStrategyResponse(strategyQuery.data).enabled,
  );
  const statusLabel = isDeleted
    ? GmvMaxTexts.statusDeleted || '已删除'
    : remoteStatusLabel === '已暂停'
      ? '已暂停'
      : isCampaignRunning
        ? strategyQuery.isLoading
          ? '状态确认中'
          : automationEnabled
            ? '自动投放中'
            : '手动投放中'
        : remoteStatusLabel;
  const statusTone = isDeleted
    ? 'muted'
    : isCampaignRunning
      ? 'success'
      : statusLabel === '已暂停'
        ? 'warning'
        : 'muted';
  const primaryProduct = campaignMetadata.products?.[0] || null;
  const selectedDateLabel = formatDateRangeLabel(metricsParams);
  const spend = metricsSummary.spend || 0;
  const gmv = metricsSummary.gmv || 0;
  const roas = spend > 0 ? gmv / spend : null;

  const summaryCards = [
    { label: '消耗', value: `$${formatMoney(spend)}` },
    { label: 'GMV', value: `$${formatMoney(gmv)}` },
    { label: '订单', value: formatNumber(metricsSummary.orders) },
    { label: 'ROAS', value: formatRoas(roas) },
  ];

  const strategyHighlights = useMemo(() => {
    const minClicks = strategyDraft.thresholds?.min_clicks ?? '';
    const minRoi = strategyDraft.thresholds?.min_roi ?? '';
    const pauseRoi = strategyDraft.thresholds?.auto_pause_roi_threshold ?? '';
    return {
      automation: strategyDraft.enabled ? '已开启' : '未开启',
      autoHeating: strategyDraft.autoHeatingEnabled ? '自动加热开启' : '自动加热关闭',
      heatingRule: `触发加热：点击 ≥ ${minClicks || '—'} 且 ROI ≥ ${minRoi || '—'}`,
      pauseRule: `自动暂停：ROI < ${pauseRoi || '—'}`,
    };
  }, [strategyDraft]);

  const missingProductFiltersMessage = productFiltersReady
    ? ''
    : '商品维度数据需要系列配置完成后才能展示。';
  const missingCreativeFiltersMessage = creativeFiltersReady
    ? ''
    : campaignQuery.isSuccess && campaignFilterId && campaignStoreId
      ? '商品关联未解析，已停止加载创意报表，避免将缺失数据错误显示为 0。请先同步系列详情。'
      : '创意维度数据需要广告系列和商品组配置。';

  return (
    <div className="gmvmax-campaign-detail">
      <header className="gmvmax-campaign-detail__header">
        <div className="gmvmax-campaign-detail__topbar">
          <button
            type="button"
            className="gmvmax-button gmvmax-button--ghost gmvmax-campaign-detail__back"
            onClick={() => navigate(-1)}
          >
            <span aria-hidden="true">←</span>
            {GmvMaxTexts.back}
          </button>
          <div className="gmvmax-campaign-detail__actions">
            {!isDeleted ? (
              <button
                type="button"
                className={`gmvmax-button ${isCampaignRunning ? 'gmvmax-button--secondary' : 'gmvmax-button--primary'}`}
                onClick={isCampaignRunning ? handlePause : handleResume}
                disabled={applyActionMutation.isPending || isEnsuringFresh || isReadOnly}
              >
                {isCampaignRunning ? '暂停投放' : '启用投放'}
              </button>
            ) : null}
            <details className="gmvmax-campaign-detail__more">
              <summary className="gmvmax-button gmvmax-button--ghost">更多操作</summary>
              <div className="gmvmax-campaign-detail__more-menu">
                <button
                  type="button"
                  onClick={() => openBudgetDialog('increase')}
                  disabled={applyActionMutation.isPending || isEnsuringFresh || isReadOnly}
                >
                  提升预算
                </button>
                <button
                  type="button"
                  onClick={() => openBudgetDialog('decrease')}
                  disabled={applyActionMutation.isPending || isEnsuringFresh || isReadOnly}
                >
                  降低预算
                </button>
                <button type="button" onClick={openActionLogs}>
                  {GmvMaxTexts.actionLogs}
                </button>
                <button
                  type="button"
                  className="danger"
                  onClick={handleDelete}
                  disabled={applyActionMutation.isPending || isEnsuringFresh || isReadOnly}
                >
                  删除系列
                </button>
              </div>
            </details>
          </div>
        </div>

        <div className="gmvmax-campaign-detail__identity">
          <div className="gmvmax-campaign-detail__header-main">
            <span className={`gmvmax-status-pill gmvmax-status-pill--${statusTone}`}>
              <span className="gmvmax-status-pill__dot" aria-hidden="true" />
              {statusLabel}
            </span>
            <h1 title={campaignMetadata.name || `系列 ${campaignId}`}>
              {campaignMetadata.name || `系列 ${campaignId}`}
            </h1>
            <span className="gmvmax-campaign-detail__campaign-id">ID {campaignId}</span>
          </div>

          <div className="gmvmax-campaign-detail__product-summary">
            <div className="gmvmax-campaign-detail__product-image">
              {primaryProduct?.image ? (
                <img src={primaryProduct.image} alt={primaryProduct.name || '商品'} />
              ) : (
                <span>暂无图片</span>
              )}
            </div>
            <div>
              <span>{campaignMetadata.products?.length || 0} 个商品</span>
              <strong title={primaryProduct?.name || ''}>
                {primaryProduct?.name || '未获取商品信息'}
              </strong>
            </div>
          </div>
        </div>

        <div className="gmvmax-campaign-detail__info">
          <dl>
            <div>
              <dt>店铺</dt>
              <dd>{storeDisplayName}</dd>
            </div>
            <div>
              <dt>广告主</dt>
              <dd>{advertiserDisplayName}</dd>
            </div>
            <div>
              <dt>授权账户</dt>
              <dd>{accountDisplayName}</dd>
            </div>
          </dl>
          <details className="gmvmax-campaign-detail__technical">
            <summary>查看技术信息</summary>
            <dl>
              <div>
                <dt>店铺 ID</dt>
                <dd>{campaignStoreId || storeIdFromQuery || '—'}</dd>
              </div>
              <div>
                <dt>广告主 ID</dt>
                <dd>{advertiserId || '—'}</dd>
              </div>
              <div>
                <dt>授权账户 ID</dt>
                <dd>{authId || '—'}</dd>
              </div>
              <div>
                <dt>工作区 / 渠道</dt>
                <dd>{workspaceId} / {provider || '—'}</dd>
              </div>
            </dl>
          </details>
        </div>
        {applyActionMutation.error ? (
          <div className="gmvmax-error">{applyActionMutation.error.message || '操作失败'}</div>
        ) : null}
      </header>

      {notifications.notification ? (
        <div className="gmvmax-notice" role="status">
          <strong>{GmvMaxTexts.newActionNotification}：</strong>
          <span>{notifications.notification.message}</span>
          <button type="button" onClick={notifications.dismiss} className="gmvmax-notice__dismiss">
            我知道了
          </button>
        </div>
      ) : null}

      {isReadOnly ? (
        <div className="gmvmax-status-banner gmvmax-status-banner--muted">
          该 GMV Max 系列已在远端删除，当前为只读视图。
        </div>
      ) : null}

      <section className="gmvmax-summary">
        <div className="gmvmax-summary__heading">
          <h2>{GmvMaxTexts.performanceSummary}</h2>
          <span>{selectedDateLabel}</span>
        </div>
        <div className="gmvmax-summary__cards">
          {summaryCards.map((card) => (
            <div key={card.label} className="gmvmax-summary__card">
              <span className="gmvmax-summary__card-label">{card.label}</span>
              <strong className="gmvmax-summary__card-value">{card.value}</strong>
            </div>
          ))}
        </div>
      </section>

      <div className="gmvmax-tabs">
        <button
          type="button"
          className={activeTab === 'automation' ? 'active' : ''}
          onClick={() => handleTabChange('automation')}
        >
          {GmvMaxTexts.automation}
        </button>
        <button
          type="button"
          className={activeTab === 'dashboard' ? 'active' : ''}
          onClick={() => handleTabChange('dashboard')}
        >
          {GmvMaxTexts.dashboard}
        </button>
        <button
          type="button"
          className={activeTab === 'products' ? 'active' : ''}
          onClick={() => handleTabChange('products')}
        >
          {GmvMaxTexts.manageProducts}
        </button>
      </div>

      {activeTab === 'automation' ? (
        <section className="gmvmax-automation gmvmax-automation--pro">
          <div className="gmvmax-automation__header">
            <div>
              <h2>智能策略</h2>
              <p className="gmvmax-automation__hint">
                每 3 分钟监控 GMV Max 表现，按 ROI、消耗和订单自动执行暂停、恢复和预算调整。
              </p>
            </div>
            {strategyQuery.isFetching ? <Loading text="策略加载中" /> : null}
            {strategyQuery.error ? (
              <div className="gmvmax-error">{strategyQuery.error.message || '策略加载失败'}</div>
            ) : null}
          </div>

          <div className="gmvmax-automation__summary gmvmax-automation__summary--pro">
            <div>
              <strong>当前状态</strong>
              <span>{strategyHighlights.automation}</span>
              <span>{strategyHighlights.autoHeating}</span>
            </div>
            <div className="gmvmax-automation__summary-details">
              <span>{strategyHighlights.heatingRule}</span>
              <span>{strategyHighlights.pauseRule}</span>
            </div>
            {strategyDraft.smartGuardState?.last_decision ? (
              <div className="gmvmax-automation__decision">
                <span>最近决策</span>
                <strong>{strategyDraft.smartGuardState.last_decision.action || 'HOLD'}</strong>
                <em>{strategyDraft.smartGuardState.last_decision.reason || '暂无原因'}</em>
              </div>
            ) : null}
          </div>

          <div className="gmvmax-automation__grid gmvmax-automation__grid--pro">
            <div className="gmvmax-automation__column">
              <FormField label="启用策略">
                <input
                  type="checkbox"
                  checked={Boolean(strategyDraft.enabled)}
                  onChange={(event) => handleStrategyFieldChange('enabled', event.target.checked)}
                />
              </FormField>
              <FormField label="自动加热">
                <input
                  type="checkbox"
                  checked={Boolean(strategyDraft.autoHeatingEnabled)}
                  onChange={(event) => handleStrategyFieldChange('autoHeatingEnabled', event.target.checked)}
                />
              </FormField>
              <FormField label="监控间隔（分钟）">
                <input
                  type="number"
                  min={MIN_MONITORING_INTERVAL}
                  value={strategyDraft.monitorIntervalMinutes}
                  onChange={(event) =>
                    handleStrategyFieldChange(
                      'monitorIntervalMinutes',
                      Math.max(MIN_MONITORING_INTERVAL, Number(event.target.value) || MIN_MONITORING_INTERVAL),
                    )
                  }
                />
              </FormField>
              <FormField label="冷却时间（分钟）">
                <input
                  type="number"
                  min={MIN_MONITORING_INTERVAL}
                  value={strategyDraft.cooldownMinutes}
                  onChange={(event) => handleStrategyFieldChange('cooldownMinutes', event.target.value)}
                />
              </FormField>
              <FormField label="评估窗口（分钟）">
                <input
                  type="number"
                  min={MIN_MONITORING_INTERVAL}
                  value={strategyDraft.evaluationWindowMinutes}
                  onChange={(event) => handleStrategyFieldChange('evaluationWindowMinutes', event.target.value)}
                />
              </FormField>
            </div>

            <div className="gmvmax-automation__column">
              <FormField label="自动暂停 ROI 阈值">
                <input
                  type="number"
                  step="0.01"
                  value={strategyDraft.thresholds.auto_pause_roi_threshold ?? ''}
                  onChange={(event) => handleThresholdChange('auto_pause_roi_threshold', event.target.value)}
                />
              </FormField>
              <FormField label="最低 ROAS">
                <input
                  type="number"
                  step="0.01"
                  value={strategyDraft.thresholds.min_roi ?? ''}
                  onChange={(event) => handleThresholdChange('min_roi', event.target.value)}
                />
              </FormField>
              <FormField label="目标 ROAS">
                <input
                  type="number"
                  step="0.01"
                  value={strategyDraft.thresholds.target_roi ?? ''}
                  onChange={(event) => handleThresholdChange('target_roi', event.target.value)}
                />
              </FormField>
              <FormField label="最小有效消耗（美元）">
                <input
                  type="number"
                  min="0"
                  step="0.5"
                  value={strategyDraft.minSpendDollars ?? ''}
                  onChange={(event) => handleStrategyFieldChange('minSpendDollars', event.target.value)}
                />
              </FormField>
              <FormField label="首次调整前运行分钟">
                <input
                  type="number"
                  min="0"
                  value={strategyDraft.minRuntimeMinutes ?? ''}
                  onChange={(event) => handleStrategyFieldChange('minRuntimeMinutes', event.target.value)}
                />
              </FormField>
            </div>

            <div className="gmvmax-automation__column">
              <FormField label="预算节奏">
                <input
                  type="checkbox"
                  checked={Boolean(strategyDraft.pacingEnabled)}
                  onChange={(event) => handleStrategyFieldChange('pacingEnabled', event.target.checked)}
                />
              </FormField>
              <FormField label="Hermes 智能建议">
                <input
                  type="checkbox"
                  checked={Boolean(strategyDraft.hermesEnabled)}
                  onChange={(event) => handleStrategyFieldChange('hermesEnabled', event.target.checked)}
                />
              </FormField>
              <FormField label="最小点击数">
                <input
                  type="number"
                  min="0"
                  value={strategyDraft.thresholds.min_clicks ?? ''}
                  onChange={(event) => handleThresholdChange('min_clicks', event.target.value)}
                />
              </FormField>
              <FormField label="每日最大提预算 (%)">
                <input
                  type="number"
                  min="0"
                  step="0.1"
                  value={strategyDraft.thresholds.max_budget_raise_pct_per_day ?? ''}
                  onChange={(event) => handleThresholdChange('max_budget_raise_pct_per_day', event.target.value)}
                />
              </FormField>
              <FormField label="每日最大降预算 (%)">
                <input
                  type="number"
                  min="0"
                  step="0.1"
                  value={strategyDraft.thresholds.max_budget_cut_pct_per_day ?? ''}
                  onChange={(event) => handleThresholdChange('max_budget_cut_pct_per_day', event.target.value)}
                />
              </FormField>
            </div>
          </div>

          <section className="gmvmax-automation__rules">
            <div className="gmvmax-automation__rules-header">
              <div>
                <h3>高级规则</h3>
                <p className="gmvmax-automation__hint">可按消耗、订单、点击、ROAS 等指标组合设置自动操作。</p>
              </div>
              <button type="button" onClick={() => setShowAdvancedRules((prev) => !prev)}>
                {showAdvancedRules ? '收起' : '展开'}
              </button>
            </div>
            {showAdvancedRules ? (
              <div className="gmvmax-automation__rules-list">
                <div className="gmvmax-automation__rules-actions">
                  <button type="button" onClick={handleAddRule}>添加规则</button>
                </div>
                {(strategyDraft.rules || []).length === 0 ? (
                  <p>暂无自定义规则，系统将按默认智能策略执行。</p>
                ) : (
                  strategyDraft.rules.map((rule, index) => (
                    <StrategyRuleEditor
                      key={rule.id || index}
                      rule={rule}
                      onChange={(nextRule) => handleRuleChange(index, nextRule)}
                      onRemove={() => handleRemoveRule(index)}
                    />
                  ))
                )}
              </div>
            ) : null}
          </section>

          <div className="gmvmax-automation__footer">
            <div className="gmvmax-automation__footer-left">
              {lastSaveMessage ? <span>{lastSaveMessage}</span> : null}
            </div>
            <div className="gmvmax-automation__footer-actions">
              <button type="button" onClick={handleStrategyReset} disabled={isReadOnly || !strategyDirty}>重置</button>
              <button type="button" onClick={handleStrategyPreview} disabled={previewStrategyMutation.isPending || isReadOnly}>预览</button>
              <button
                type="button"
                className="primary"
                onClick={handleStrategySave}
                disabled={updateStrategyMutation.isPending || !strategyDirty || isReadOnly}
              >
                {updateStrategyMutation.isPending ? '保存中...' : '保存策略'}
              </button>
            </div>
          </div>

          {previewStrategyMutation.error ? (
            <div className="gmvmax-error">{previewStrategyMutation.error.message || '预览失败'}</div>
          ) : null}
          {latestPreviewResult ? (
            <div className="gmvmax-preview-result">
              <h4>预览结果</h4>
              <pre>{JSON.stringify(latestPreviewResult, null, 2)}</pre>
            </div>
          ) : null}
        </section>
      ) : null}

      {activeTab === 'products' ? (
        <section className="gmvmax-products">
          <div className="gmvmax-products__header">
            <div>
              <h2>{GmvMaxTexts.manageProducts}</h2>
              <p className="gmvmax-products__summary">已绑定 {initialProductSet.size} 个商品</p>
            </div>
            {productMessage ? <div className="gmvmax-notice">{productMessage}</div> : null}
            {productsQuery.error ? (
              <div className="gmvmax-error">{productsQuery.error.message || '商品加载失败'}</div>
            ) : null}
          </div>
          <ProductSelectionPanel
            products={mergedProducts}
            selectedIds={productSelection}
            onToggle={toggleProduct}
            onToggleAll={toggleAllProducts}
            storeNames={storeNameMap}
            loading={productsQuery.isLoading || productsQuery.isFetching}
            emptyMessage="暂无可管理的商品。"
            disabled={updateProductsMutation.isPending || isReadOnly}
            onSelectAll={selectAllProducts}
            onClearAll={clearProducts}
            searchTerm={productSearch}
            onSearchChange={setProductSearch}
          />
          <div className="gmvmax-products__actions">
            <span>已选 {productSelection.size} 个商品</span>
            <button
              type="button"
              onClick={() => setProductSelection(new Set(initialProductSet))}
              disabled={isReadOnly}
            >
              重置
            </button>
            <button
              type="button"
              className="primary"
              disabled={!productsChanged || updateProductsMutation.isPending || isReadOnly}
              onClick={handleSaveProducts}
            >
              保存商品
            </button>
          </div>
        </section>
      ) : null}
      {activeTab === 'dashboard' ? (
        <section className="gmvmax-dashboard">
          <div className="gmvmax-dashboard__controls">
            <div className="gmvmax-dashboard__range-block">
              <div className="gmvmax-dashboard__range">
                {[
                  { key: 'today', label: '今日' },
                  { key: '7d', label: '近 7 日' },
                  { key: '30d', label: '近 30 日' },
                  { key: 'custom', label: '自定义' },
                ].map((option) => (
                  <button
                    key={option.key}
                    type="button"
                    className={`gmvmax-chip ${timeRange === option.key ? 'gmvmax-chip--active' : ''}`}
                    onClick={() => handleTimeRangeChange(option.key)}
                  >
                    {option.label}
                  </button>
                ))}
                {timeRange === 'custom' ? (
                  <div className="gmvmax-dashboard__custom-range">
                    <input
                      type="date"
                      aria-label="开始日期"
                      value={customRange.start}
                      onChange={(event) => handleCustomRangeChange('start', event.target.value)}
                    />
                    <span>至</span>
                    <input
                      type="date"
                      aria-label="结束日期"
                      value={customRange.end}
                      onChange={(event) => handleCustomRangeChange('end', event.target.value)}
                    />
                  </div>
                ) : null}
              </div>
              <strong className="gmvmax-dashboard__selected-dates">{selectedDateLabel}</strong>
            </div>
            <div className="gmvmax-dashboard__controls-right">
              <div className="gmvmax-dashboard__timezone">
                {hasAdvertiserTimezone ? '按广告主时区' : '广告主时区未知，暂按浏览器时区'}：
                {advertiserTimezone}
              </div>
              <button
                type="button"
                className="gmvmax-button gmvmax-button--secondary"
                onClick={handleSyncMetrics}
                disabled={metricsSync.isSyncing || isReadOnly}
              >
                {metricsSync.isSyncing
                  ? metricsSync.syncState === 'RETRY'
                    ? '等待同步…'
                    : '同步中…'
                  : '同步数据'}
              </button>
              {metricsSync.isSyncing && metricsSync.syncMessage ? (
                <span className="gmvmax-dashboard__sync-progress" role="status" aria-live="polite">
                  {metricsSync.syncMessage}
                </span>
              ) : null}
            </div>
          </div>
          {(campaignFreshness || creativeFreshness) ? (
            <div className="gmvmax-data-freshness-list">
              {campaignFreshness ? (
                <div className={`gmvmax-data-freshness gmvmax-data-freshness--${campaignFreshness.state}`}>
                  <strong>系列数据：{campaignFreshness.label}</strong>
                  <span>{campaignFreshness.detail}</span>
                </div>
              ) : null}
              {creativeFreshness ? (
                <div className={`gmvmax-data-freshness gmvmax-data-freshness--${creativeFreshness.state}`}>
                  <strong>素材数据：{creativeFreshness.label}</strong>
                  <span>{creativeFreshness.detail}</span>
                </div>
              ) : null}
            </div>
          ) : null}
          {campaignMetricsQuery.isFetching || productMetricsQuery.isFetching ? (
            <Loading text="数据加载中..." />
          ) : null}
          {!productFiltersReady ? (
            <div className="gmvmax-text-muted">{missingProductFiltersMessage}</div>
          ) : summaryErrorMessage ? (
            <div className={isMissingFilterError(summaryError) ? 'gmvmax-text-muted' : 'gmvmax-error'}>
              {summaryErrorMessage}
            </div>
          ) : null}

          <div className="gmvmax-summary__cards gmvmax-summary__cards--muted">
            {summaryCards.map((card) => (
              <div key={card.label} className="gmvmax-summary__card">
                <span className="gmvmax-summary__card-label">{card.label}</span>
                <strong className="gmvmax-summary__card-value">{card.value}</strong>
              </div>
            ))}
          </div>

          <div className="gmvmax-dashboard__chart">
            <div className="gmvmax-dashboard__chart-header">
              <div>
                <h3>投放趋势</h3>
                <span className="gmvmax-dashboard__chart-caption">{selectedDateLabel}</span>
              </div>
              <div className="gmvmax-trend-metrics" aria-label="趋势指标">
                {TREND_METRIC_OPTIONS.map((option) => (
                  <label
                    key={option.key}
                    className={`gmvmax-trend-metric ${trendMetrics.has(option.key) ? 'is-active' : ''}`}
                  >
                    <input
                      type="checkbox"
                      checked={trendMetrics.has(option.key)}
                      onChange={() => handleToggleTrendMetric(option.key)}
                    />
                    <i style={{ backgroundColor: option.color }} />
                    <span>{option.label}</span>
                  </label>
                ))}
              </div>
            </div>
            <TrendChart data={trendSeries} selectedMetrics={trendMetrics} />
          </div>

          <div className="gmvmax-dashboard__table gmvmax-dashboard__table--creatives">
            {seedCreatives.length > 0 ? (
              <div className="gmvmax-seed-panel">
                <div className="gmvmax-seed-panel__header">
                  <div className="gmvmax-seed-panel__title">
                    <span>种子创意 ({seedCreatives.length})</span>
                  </div>
                  <span className="gmvmax-seed-panel__hint" title={SEED_RULE_TEXT}>
                    规则：{SEED_RULE_TEXT}
                  </span>
                </div>
                <div className="gmvmax-seed-panel__grid">
                  {seedCreatives.slice(0, 6).map((creative) => (
                    <div key={creative.rowKey || creative.creativeId} className="gmvmax-seed-card">
                      <div className="gmvmax-seed-card__name" title={creative.name}>
                        种子：{creative.name}
                      </div>
                      <div className="gmvmax-seed-card__meta">
                        <span>ROAS {formatNumber(creative.metrics?.roas, { maximumFractionDigits: 2 })}</span>
                        <span>转化 {formatNumber(creative.metrics?.orders)}</span>
                        <span>消耗 ${formatMoney(creative.metrics?.spend)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            <div className="gmvmax-creatives__filters">
              <div className="gmvmax-chip-group" aria-label="创意状态筛选">
                {creativeStatusOptions.map((option) => (
                  <button
                    key={option.key}
                    type="button"
                    className={`gmvmax-chip ${creativeStatusFilters.has(option.key) ? 'gmvmax-chip--active' : ''}`}
                    onClick={() => handleToggleCreativeStatus(option.key)}
                  >
                    {option.label}
                    <span className="gmvmax-chip__count">{creativeStatusCounts.get(option.key) || 0}</span>
                  </button>
                ))}
              </div>
              <div className="gmvmax-creatives__toggles">
                <label className="gmvmax-checkbox gmvmax-checkbox--inline">
                  <input
                    type="checkbox"
                    checked={showOnlyHeated}
                    onChange={(event) => setShowOnlyHeated(event.target.checked)}
                  />
                  <span>仅显示加热中</span>
                </label>
                <label className="gmvmax-checkbox gmvmax-checkbox--inline">
                  <input
                    type="checkbox"
                    checked={showSeedOnly}
                    onChange={(event) => setShowSeedOnly(event.target.checked)}
                  />
                  <span>仅种子创意</span>
                </label>
                <div className="gmvmax-sort-control">
                  <span className="gmvmax-sort-control__label">排序指标</span>
                  <label className="gmvmax-sort-control__select">
                    <select
                      aria-label="排序指标"
                      value={creativeSortKey}
                      onChange={(event) => setCreativeSortKey(event.target.value)}
                    >
                      {CREATIVE_SORT_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="gmvmax-sort-control__direction"
                    onClick={() => setCreativeSortDirection((current) => (current === 'desc' ? 'asc' : 'desc'))}
                    aria-label={creativeSortDirection === 'desc' ? '当前降序，点击切换升序' : '当前升序，点击切换降序'}
                  >
                    <span aria-hidden="true">{creativeSortDirection === 'desc' ? '↓' : '↑'}</span>
                    {creativeSortDirection === 'desc' ? '降序' : '升序'}
                  </button>
                </div>
              </div>
            </div>
            {creativesLoading ? <Loading text="创意加载中..." /> : null}
            {!creativeFiltersReady ? (
              <div className="gmvmax-error">{missingCreativeFiltersMessage}</div>
            ) : creativesErrorMessage ? (
              <div
                className={
                  isMissingFilterError(creativesError) ? 'gmvmax-text-muted' : 'gmvmax-error'
                }
              >
                创意数据加载失败：{creativesErrorMessage}
              </div>
            ) : null}
            {creativeActionError ? (
              <div className="gmvmax-error">
                创意操作失败：{creativeActionError.message || '请稍后重试'}
              </div>
            ) : null}
            <div className="table-wrap table-wrap--creatives">
              <table className="gmvmax-table gmvmax-creatives-table">
                <thead>
                  <tr>
                    <th>创意</th>
                    <th>状态</th>
	                    <th className="gmvmax-metric-group-start">消耗</th>
	                    <th>CPM</th>
	                    <th>CPC</th>
	                    <th className="gmvmax-metric-group-start">GMV</th>
	                    <th>ROAS</th>
	                    <th>曝光</th>
	                    <th>点击</th>
	                    <th>CTR</th>
	                    <th>转化</th>
	                    <th>转化率</th>
	                    <th className="gmvmax-metric-group-start">2秒观看率</th>
	                    <th>6秒观看率</th>
	                    <th>完播率</th>
	                    <th className="gmvmax-metric-group-start">广告流占比</th>
	                    <th>自然流占比</th>
                    <th>标签</th>
                    <th className="col-actions">操作</th>
                  </tr>
                  <tr className="gmvmax-creatives__totals-row">
                    <th>当前筛选合计（{sortedCreatives.length}）</th>
                    <th>全部状态</th>
                    <th className="gmvmax-metric-group-start">${formatMoney(creativeMetricsTotals.spend)}</th>
                    <th>${formatMoney(creativeMetricsTotals.cpm)}</th>
                    <th>{creativeMetricsTotals.clicks > 0 ? `$${formatMoney(creativeMetricsTotals.cpc)}` : '—'}</th>
                    <th className="gmvmax-metric-group-start">${formatMoney(creativeMetricsTotals.gmv)}</th>
                    <th>{formatRoas(creativeMetricsTotals.roas)}</th>
                    <th>{formatNumber(creativeMetricsTotals.impressions)}</th>
                    <th>{formatNumber(creativeMetricsTotals.clicks)}</th>
                    <th>{formatRate(creativeMetricsTotals.ctr)}</th>
                    <th>{formatNumber(creativeMetricsTotals.orders)}</th>
                    <th>{formatRate(creativeMetricsTotals.conversionRate)}</th>
                    <th className="gmvmax-metric-group-start">{formatRate(creativeMetricsTotals.video2sRate)}</th>
                    <th>{formatRate(creativeMetricsTotals.video6sRate)}</th>
                    <th>{formatRate(creativeMetricsTotals.completionRate)}</th>
                    <th className="gmvmax-metric-group-start">{formatRate(creativeMetricsTotals.adFlowShare)}</th>
                    <th>{formatRate(creativeMetricsTotals.organicFlowShare)}</th>
                    <th>—</th>
                    <th className="col-actions">—</th>
                  </tr>
                </thead>
                <tbody>
                  {creativesByStatus.length === 0 ? (
                    <tr>
                      <td colSpan={19}>
                        {creativesLoading
                          ? '创意数据同步中...'
                          : creatives.length === 0
                            ? '暂无创意级数据。请点击同步数据，系统会读取最新素材表现快照。'
                            : '当前筛选条件下暂无素材。'}
                      </td>
                    </tr>
                  ) : null}
                  {creativesByStatus.map((group) => (
                    <Fragment key={group.key}>
                      <tr className="gmvmax-creatives__group-row">
                        <td colSpan={19}>
                          {group.label} <span className="gmvmax-text-muted">({group.items.length})</span>
                        </td>
                      </tr>
                      {group.items.map((creative) => {
                        const metrics = creative.metrics || {};
                        const boosting = isCreativeBoosting(creative);
                        const isSeed = isSeedCreative(creative);
                        const historicalMetrics = creative.historicalMetrics || {};
                        const hasHistoricalPerformance =
                          creative.status === 'CANDIDATE' && Number(historicalMetrics.spend || 0) > 0;
                        const isProductCard = creative.creativeId === '-1' || creative.creativeId === -1;
                        const displayCreativeId = isProductCard ? '商品卡' : creative.creativeId;
                        const boundProducts = ensureArray(campaignMetadata.products);
                        const creativeProductId = String(
                          creative.metadata?.product_id ||
                            creative.metadata?.item_group_id ||
                            creative.metadata?.itemGroupId ||
                            '',
                        );
                        const fallbackProduct = isProductCard
                          ? boundProducts.find(
                              (product) =>
                                creativeProductId &&
                                String(getProductIdentifier(product)) === creativeProductId,
                            ) || boundProducts[0]
                          : null;
                        // TikTok reports the product card as creative_id=-1. It is
                        // not a video creative, so never let cached video media
                        // override the product catalog thumbnail.
                        const thumbnail = isProductCard
                          ? fallbackProduct?.image || null
                          : creative.thumbnail;
                        const previewUrl = isProductCard ? null : creative.previewUrl;
                        const fullCreativeName =
                          isProductCard && fallbackProduct?.name ? `商品卡 · ${fallbackProduct.name}` : creative.name;
                        const creativeName = truncateText(fullCreativeName, isProductCard ? 34 : 44);
                        const coverNode = thumbnail ? (
                          <img
                            src={thumbnail}
                            alt={fullCreativeName || '创意封面'}
                            className="gmvmax-creatives__thumb"
                            loading="lazy"
                            decoding="async"
                            fetchPriority="low"
                          />
                        ) : (
                          <span className="gmvmax-creatives__thumb gmvmax-creatives__thumb--placeholder">封面</span>
                        );
                        return (
                          <tr key={creative.rowKey || creative.creativeId}>
                            <td>
                              <div className="gmvmax-creatives__name">
                                {previewUrl ? (
                                  <a
                                    className="gmvmax-creatives__preview-link"
                                    href={previewUrl}
                                    target="_blank"
                                    rel="noreferrer"
                                    title="打开视频预览"
                                  >
                                    {coverNode}
                                  </a>
                                ) : (
                                  coverNode
                                )}
                                <div>
                                  <div className="gmvmax-creatives__label" title={fullCreativeName}>
                                    {creativeName}
                                  </div>
                                  <div className="gmvmax-creatives__id">ID: {displayCreativeId}</div>
                                  {creative.metadata?.product_id ? (
                                    <div className="gmvmax-creatives__id">
                                      商品: {creative.metadata.product_id}
                                    </div>
                                  ) : null}
                                </div>
                              </div>
                            </td>
                            <td>
                              <span className="gmvmax-status-pill gmvmax-status-pill--muted">
                                {creativeStatusLabelMap.get(creative.status || 'CANDIDATE') || '候选素材'}
                              </span>
                            </td>
	                            <td className="gmvmax-metric-group-start">${formatMoney(metrics.spend)}</td>
	                            <td>${formatMoney(metrics.cpm)}</td>
	                            <td>{Number(metrics.clicks || 0) > 0 ? `$${formatMoney(metrics.cpc)}` : '—'}</td>
	                            <td className="gmvmax-metric-group-start">${formatMoney(metrics.gmv)}</td>
	                            <td>{formatRoas(metrics.roas)}</td>
	                            <td>{formatNumber(metrics.impressions)}</td>
	                            <td>{formatNumber(metrics.clicks)}</td>
	                            <td>{formatRate(metrics.ctr)}</td>
	                            <td>{formatNumber(metrics.orders)}</td>
	                            <td>{formatRate(metrics.conversionRate)}</td>
	                            <td className="gmvmax-metric-group-start">{formatRate(metrics.video2sRate)}</td>
	                            <td>{formatRate(metrics.video6sRate)}</td>
	                            <td>{formatRate(metrics.completionRate)}</td>
	                            <td className="gmvmax-metric-group-start">{formatRate(metrics.adFlowShare)}</td>
	                            <td>{formatRate(metrics.organicFlowShare)}</td>
                            <td>
                              <div className="gmvmax-tag-list">
                                {boosting ? (
                                  <span className="gmvmax-tag gmvmax-tag--heat" title="加热中">
                                    加热中
                                  </span>
                                ) : null}
                                {isSeed ? (
                                  <span className="gmvmax-tag" title={SEED_RULE_TEXT}>
                                    种子
                                  </span>
                                ) : null}
                                {hasHistoricalPerformance ? (
                                  <span
                                    className="gmvmax-tag"
                                    title={`近30天跨系列历史消耗 $${formatMoney(historicalMetrics.spend)}，历史 ROAS ${formatRoas(historicalMetrics.roas)}`}
                                  >
                                    近30天 ROAS {formatRoas(historicalMetrics.roas)}
                                  </span>
                                ) : null}
                                {creative.metadata?.historically_excluded ? (
                                  <span className="gmvmax-tag" title="该素材曾在其他系列中被策略排除">
                                    历史排除
                                  </span>
                                ) : null}
                              </div>
                            </td>
                            <td className="col-actions">
                              {HEATABLE_CREATIVE_STATUSES.has(creative.status) ? (
                                <div className="gmvmax-series-actions">
                                  {boosting ? (
                                    <button
                                      type="button"
                                      className="gmvmax-button gmvmax-button--ghost"
                                      onClick={() => handleStopHeat(creative)}
                                      disabled={creativeActionPending || isReadOnly}
                                    >
                                      停止加热
                                    </button>
                                  ) : (
                                    <button
                                      type="button"
                                      className="gmvmax-button gmvmax-button--ghost"
                                      onClick={() => handleBoostCreative(creative)}
                                      disabled={creativeActionPending || isReadOnly}
                                    >
                                      加热
                                    </button>
                                  )}
                                </div>
                              ) : (
                                <span className="gmvmax-creatives__badge gmvmax-creatives__badge--muted">无操作</span>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="gmvmax-dashboard__table">
            <h3>{GmvMaxTexts.products}</h3>
            <table>
              <thead>
                <tr>
                  <th>商品</th>
                  <th>消耗</th>
                  <th>GMV</th>
                  <th>订单</th>
                  <th>点击</th>
                  <th>ROAS</th>
                </tr>
              </thead>
              <tbody>
                {productTable.length === 0 ? (
                  <tr>
                    <td colSpan={6}>暂无商品数据。</td>
                  </tr>
                ) : (
                  productTable
                    .slice()
                    .sort((a, b) => b.gmv - a.gmv)
                    .map((row) => {
                      const rowRoas = row.spend > 0 ? row.gmv / row.spend : null;
                      return (
                        <tr key={row.id}>
                          <td>
                            <div className="gmvmax-product-cell">
                              {row.image ? (
                                <img className="gmvmax-product-cell__image" src={row.image} alt={row.name || row.id} />
                              ) : null}
                              <div>
                                <div title={row.name && row.name !== row.id ? row.name : ''}>
                                  {row.name && row.name !== row.id ? truncateText(row.name, 38) : `商品 ${row.id}`}
                                </div>
                                <div className="gmvmax-text-muted">ID: {row.id}</div>
                              </div>
                            </div>
                          </td>
                          <td>${formatMoney(row.spend)}</td>
                          <td>${formatMoney(row.gmv)}</td>
                          <td>{formatNumber(row.orders)}</td>
                          <td>{formatNumber(row.clicks)}</td>
                          <td>{rowRoas === null ? '—' : rowRoas.toFixed(2)}</td>
                        </tr>
                      );
                    })
                )}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
      <BudgetDialog
        open={budgetDialog.open}
        mode={budgetDialog.mode}
        onClose={closeBudgetDialog}
        onSubmit={handleBudgetSubmit}
      />

      <Modal open={actionLogsOpen} title={GmvMaxTexts.campaignActionLogs} onClose={closeActionLogs}>
        {actionLogsOpen ? (
          <ActionLogsTable
            workspaceId={workspaceId}
            provider={provider}
            authId={authId}
            campaignId={campaignId}
          />
        ) : null}
      </Modal>
    </div>
  );
}
