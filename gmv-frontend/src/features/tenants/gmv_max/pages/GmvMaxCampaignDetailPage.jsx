import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';

import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';
import Modal from '@/components/ui/Modal.jsx';

import {
  useApplyGmvMaxActionMutation,
  useGmvMaxCampaignCreativesQuery,
  useGmvMaxCampaignQuery,
  useProductsQuery,
  useGmvMaxCreativeHeatingQuery,
  useGmvMaxCreativeMetricsQuery,
  useGmvMaxMetricsQuery,
  useGmvMaxStrategyQuery,
  usePreviewGmvMaxStrategyMutation,
  useSyncGmvMaxMetricsMutation,
  useUpdateGmvMaxStrategyMutation,
} from '../hooks/gmvMaxQueries.js';
import { useEnsureFreshGmvData } from '../hooks/useGmvSyncTask.js';
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
} from './gmvMaxOverview/helpers.js';

const MIN_MONITORING_INTERVAL = 10;
const METRIC_CHOICES = [
  { value: 'roi', label: 'ROAS' },
  { value: 'spend', label: '消耗' },
  { value: 'gmv', label: 'GMV' },
  { value: 'orders', label: '订单' },
  { value: 'ctr', label: 'CTR' },
  { value: 'cpc', label: 'CPC' },
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
      cpc: 0,
      roas: 0,
    };
  }
  const spend = Number(metrics.cost ?? metrics.net_cost ?? metrics.spend ?? 0) || 0;
  const gmv = Number(metrics.gross_revenue ?? metrics.gmv ?? metrics.revenue ?? 0) || 0;
  const clicks = Number(
    metrics.product_clicks ?? metrics.clicks ?? metrics.total_clicks ?? metrics.ad_clicks ?? 0,
  ) || 0;
  const impressions = Number(metrics.product_impressions ?? metrics.impressions ?? metrics.views ?? 0) || 0;
  const orders = Number(metrics.orders ?? metrics.total_orders ?? metrics.conversions ?? 0) || 0;
  const ctr = metrics.ctr ?? metrics.click_through_rate ?? (impressions > 0 ? clicks / impressions : 0);
  const cpc = metrics.cpc ?? metrics.cost_per_click ?? (clicks > 0 ? spend / clicks : 0);
  const roas = metrics.roas ?? metrics.roi ?? (spend > 0 ? gmv / spend : 0);
  return { impressions, clicks, spend, gmv, orders, ctr, cpc, roas };
}

function ensureArray(value) {
  if (Array.isArray(value)) return value;
  if (value === undefined || value === null) return [];
  return [value];
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
    ctr: ['product_click_rate', 'ctr', 'click_through_rate'],
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
  return (
    entry.stat_time_day ||
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

function resolveMetricsError(error, defaultMessage = '数据加载失败') {
  if (!error) return '';
  if (isMissingFilterError(error)) {
    return '暂无数据，请检查广告系列和商品组配置。';
  }
  return error?.response?.data?.message || error?.message || defaultMessage;
}

function normalizeStrategyResponse(data) {
  if (!data || typeof data !== 'object') {
    return {
      enabled: true,
      autoHeatingEnabled: true,
      cooldownMinutes: 30,
      minRuntimeMinutes: 120,
      thresholds: {},
      rules: [],
      raw: data,
    };
  }
  const config = data.config_json || data.configJson || {};
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
      data.auto_pause_roi_threshold ?? config.auto_pause_roi_threshold ?? '',
  };
  return {
    enabled: Boolean(data.enabled ?? true),
    autoHeatingEnabled: Boolean(
      data.auto_heating_enabled ?? config.auto_heating_enabled ?? data.autoHeatingEnabled ?? true,
    ),
    cooldownMinutes: data.cooldown_minutes ?? data.cooldownMinutes ?? 30,
    minRuntimeMinutes:
      data.min_runtime_minutes_before_first_change ??
      data.minRuntimeMinutesBeforeFirstChange ??
      120,
    thresholds: normalizedThresholds,
    rules,
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

function buildTrendSeries(report) {
  const entries = ensureArray(report?.list);
  return entries
    .map((entry) => ({
      label: extractDateLabel(entry),
      spend: getMetricValue(entry, 'spend'),
      roas: (() => {
        const spend = getMetricValue(entry, 'spend');
        const gmv = getMetricValue(entry, 'gmv');
        return spend > 0 ? gmv / spend : 0;
      })(),
    }))
    .filter((item) => item.label);
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

function deriveCampaignMetadata(campaignData) {
  if (!campaignData) return {};
  const campaign = campaignData.campaign || campaignData;
  const sessions = ensureArray(campaignData.sessions || campaignData.session_list);
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
      products.push({
        id: id ? String(id) : undefined,
        name: product.product_name || product.title || product.name || product.item_name,
        image: product.image_url || product.cover_url || product.thumbnail_url,
      });
    }
  }
  const uniqueProducts = products.filter((item, index, list) => {
    if (!item.id) return index === list.findIndex((entry) => !entry.id);
    return index === list.findIndex((entry) => entry.id === item.id);
  });
  return {
    id: campaign.campaign_id || campaign.id,
    name: campaign.name || campaign.campaign_name || campaign.session_name,
    status: campaign.status || campaign.delivery_status || campaign.campaign_status,
    advertiserName: campaign.advertiser_name || campaign.advertiser || campaign.advertiserId,
    storeName: campaign.store_name || campaign.storeName || campaign.store_id,
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
  if (!status) return 'UNKNOWN';
  const normalized = String(status).toUpperCase();
  if (normalized.includes('QUEUE')) return 'IN_QUEUE';
  if (normalized.includes('LEARN')) return 'LEARNING';
  if (normalized.includes('DELIVER')) return 'DELIVERING';
  if (normalized.includes('NOT_DELIVER')) return 'NOT_DELIVERING';
  return normalized;
}

function normalizeCreativesData(creativesData, metricsData, heatingData) {
  const rows = new Map();

  const ensureRow = (creativeId) => {
    const key = String(creativeId);
    if (!rows.has(key)) {
      rows.set(key, {
        creativeId: key,
        name: key,
        thumbnail: null,
        status: 'UNKNOWN',
        metrics: parseCreativeMetrics({}),
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
    const row = ensureRow(creativeId);
    row.name =
      item?.title ||
      item?.creative_name ||
      item?.creativeName ||
      item?.name ||
      item?.label ||
      item?.title ||
      row.name ||
      creativeId;
    row.thumbnail = item?.thumbnail || item?.image || item?.cover_url || item?.coverUrl || row.thumbnail;
    row.status = normalizeCreativeStatus(
      item?.creative_delivery_status || item?.creative_status || item?.status || item?.state || row.status,
    );
    row.metrics = parseCreativeMetrics(item?.metrics || row.metrics);
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
      const row = ensureRow(creativeId);
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
      row.metrics = parseCreativeMetrics(entry?.metrics || entry);
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
      const row = ensureRow(creativeId);
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
      row.metrics = parseCreativeMetrics(entry.metrics || entry);
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
    const row = ensureRow(creativeId);
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

function TrendChart({ data }) {
  if (!data || data.length === 0) {
    return <div className="gmvmax-chart gmvmax-chart--empty">暂无可用数据。</div>;
  }
  const padding = 16;
  const width = 600;
  const height = 240;
  const maxValue = Math.max(
    ...data.map((point) => Math.max(point.spend || 0, point.roas || 0)),
    1,
  );
  const xStep = (width - padding * 2) / Math.max(data.length - 1, 1);
  const scaleY = (value) => height - padding - (value / maxValue) * (height - padding * 2);
  const buildPath = (key) =>
    data
      .map((point, index) => {
        const x = padding + index * xStep;
        const y = scaleY(point[key] || 0);
        return `${index === 0 ? 'M' : 'L'}${x},${y}`;
      })
      .join(' ');
  const spendPath = buildPath('spend');
  const roasPath = buildPath('roas');
  const xLabels = data.map((point, index) => ({
    x: padding + index * xStep,
    label: point.label,
  }));
  return (
    <svg className="gmvmax-chart" viewBox={`0 0 ${width} ${height}`} role="img">
      <g className="gmvmax-chart__grid">
        <line x1={padding} y1={height - padding} x2={width - padding} y2={height - padding} />
        <line x1={padding} y1={padding} x2={padding} y2={height - padding} />
      </g>
      <path d={spendPath} className="gmvmax-chart__line gmvmax-chart__line--spend" />
      <path d={roasPath} className="gmvmax-chart__line gmvmax-chart__line--roas" />
      {xLabels.map((item) => (
        <text key={item.x} x={item.x} y={height - 4} textAnchor="middle" className="gmvmax-chart__label">
          {item.label}
        </text>
      ))}
    </svg>
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
  const [searchParams, setSearchParams] = useSearchParams();

  const provider = searchParams.get('provider') || '';
  const authId = searchParams.get('authId') || '';
  const advertiserId = searchParams.get('advertiserId') || '';
  const storeIdFromQuery = searchParams.get('storeId') || '';
  const advertiserTimezone = useMemo(
    () => resolveTimezoneLabel(searchParams.get('timezone') || ''),
    [searchParams],
  );

  const resolveTab = useCallback((params) => {
    const value = params.get('tab');
    if (value === 'dashboard' || value === 'products') return value;
    return 'automation';
  }, []);

  const [activeTab, setActiveTab] = useState(() => resolveTab(searchParams));
  const [timeRange, setTimeRange] = useState(searchParams.get('range') || 'today');
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
    () => new Set(['DELIVERING', 'LEARNING', 'IN_QUEUE', 'NOT_DELIVERING', 'UNKNOWN']),
  );
  const [showOnlyHeated, setShowOnlyHeated] = useState(false);
  const [showSeedOnly, setShowSeedOnly] = useState(false);
  const [creativeSortKey, setCreativeSortKey] = useState('spend');
  const [productSelection, setProductSelection] = useState(() => new Set());
  const [productSearch, setProductSearch] = useState('');
  const [productMessage, setProductMessage] = useState('');
  const queryClient = useQueryClient();

  useEffect(() => {
    setActiveTab(resolveTab(searchParams));
  }, [resolveTab, searchParams]);

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

  const commonEnabled = Boolean(workspaceId && provider && authId && campaignId);
  const { ensureFresh, isSyncing: isEnsuringFresh } = useEnsureFreshGmvData({
    workspaceId,
    provider,
    authId,
    storeId: storeIdFromQuery,
    reportParams: syncReportParams,
  });

  const campaignQuery = useGmvMaxCampaignQuery(workspaceId, provider, authId, campaignId, {
    enabled: commonEnabled,
  });

  const campaignFilterId = useMemo(() => {
    const campaign = campaignQuery.data?.campaign || campaignQuery.data;
    const resolvedId = campaign?.campaign_id || campaign?.campaignId || campaign?.id || campaignId;
    return resolvedId ? String(resolvedId) : '';
  }, [campaignId, campaignQuery.data]);

  const itemGroupId = useMemo(() => {
    const detail = campaignQuery.data;
    const campaign = detail?.campaign || detail;
    const directId =
      detail?.item_group_id ||
      detail?.itemGroupId ||
      campaign?.item_group_id ||
      campaign?.itemGroupId;
    if (directId !== undefined && directId !== null && String(directId) !== '') {
      return String(directId);
    }

    const listCandidates = [
      detail?.item_group_ids || detail?.itemGroupIds,
      campaign?.item_group_ids || campaign?.itemGroupIds,
    ];
    for (const candidate of listCandidates) {
      const first = ensureArray(candidate).find(
        (value) => value !== undefined && value !== null && String(value) !== '',
      );
      if (first !== undefined && first !== null && String(first) !== '') {
        return String(first);
      }
    }

    const sessions = ensureArray(detail?.sessions || detail?.session_list);
    for (const session of sessions) {
      const sessionItemGroup = ensureArray(session?.item_group_ids || session?.itemGroupIds).find(
        (value) => value !== undefined && value !== null && String(value) !== '',
      );
      if (sessionItemGroup !== undefined && sessionItemGroup !== null && String(sessionItemGroup) !== '') {
        return String(sessionItemGroup);
      }
      const products = ensureArray(session?.product_list || session?.products);
      for (const product of products) {
        const productItemGroupId = product?.item_group_id || product?.itemGroupId;
        if (
          productItemGroupId !== undefined &&
          productItemGroupId !== null &&
          String(productItemGroupId) !== ''
        ) {
          return String(productItemGroupId);
        }
      }
    }

    return '';
  }, [campaignQuery.data]);

  const productFiltersReady = Boolean(campaignFilterId);
  const creativeFiltersReady = Boolean(campaignFilterId && itemGroupId);

  const isDashboardTab = activeTab === 'dashboard';
  const isProductsTab = activeTab === 'products';
  const creativeStatusOptions = useMemo(
    () => [
      { key: 'DELIVERING', label: GmvMaxTexts.creativeStatusDelivering },
      { key: 'LEARNING', label: GmvMaxTexts.creativeStatusLearning },
      { key: 'IN_QUEUE', label: GmvMaxTexts.creativeStatusInQueue },
      { key: 'NOT_DELIVERING', label: GmvMaxTexts.creativeStatusNotDelivering },
      { key: 'UNKNOWN', label: GmvMaxTexts.creativeStatusUnknown },
    ],
    [],
  );

  const campaignMetricsQuery = useGmvMaxMetricsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    {
      ...metricsParams,
      advertiser_id: advertiserId || undefined,
      level: 'campaign',
      campaign_ids: campaignFilterId ? [campaignFilterId] : undefined,
    },
    {
      enabled: commonEnabled && productFiltersReady,
    },
  );

  const productMetricsQuery = useGmvMaxMetricsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    {
      ...metricsParams,
      advertiser_id: advertiserId || undefined,
      level: 'product',
      campaign_ids: campaignFilterId ? [campaignFilterId] : undefined,
    },
    {
      enabled: commonEnabled && productFiltersReady,
    },
  );

  const creativesQuery = useGmvMaxCampaignCreativesQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    {
      ...metricsParams,
      advertiser_id: advertiserId || undefined,
      level: 'creative',
      campaign_ids: campaignFilterId ? [campaignFilterId] : undefined,
      item_group_ids: itemGroupId ? [itemGroupId] : undefined,
    },
    {
      enabled: commonEnabled && isDashboardTab && creativeFiltersReady,
    },
  );

  const creativeMetricsQuery = useGmvMaxCreativeMetricsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    {
      ...metricsParams,
      advertiser_id: advertiserId || undefined,
      level: 'creative',
      campaign_ids: campaignFilterId ? [campaignFilterId] : undefined,
      item_group_ids: itemGroupId ? [itemGroupId] : undefined,
    },
    {
      enabled: commonEnabled && isDashboardTab && creativeFiltersReady,
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
  const campaignStoreId = useMemo(() => {
    const campaign = campaignQuery.data?.campaign || campaignQuery.data;
    return campaign?.store_id || campaign?.storeId || '';
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
    sessionProducts.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) set.add(String(id));
    });
    return set;
  }, [sessionProducts]);

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
      gmv_max_ads_status: 'UNOCCUPIED',
      page_size: 50,
    },
    {
      enabled: Boolean(commonEnabled && isProductsTab && (storeIdFromQuery || campaignStoreId)),
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

  const syncMetricsMutation = useSyncGmvMaxMetricsMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      campaignMetricsQuery.refetch();
      productMetricsQuery.refetch();
      creativeMetricsQuery.refetch();
    },
  });
  const applyActionMutation = useApplyGmvMaxActionMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      campaignQuery.refetch();
      campaignMetricsQuery.refetch();
      productMetricsQuery.refetch();
      creativeMetricsQuery.refetch();
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'action-logs'] });
    },
  });
  const creativeActionMutation = useApplyGmvMaxActionMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'creative-metrics'] });
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'creative-heating'] });
      queryClient.invalidateQueries({ queryKey: ['gmvMax', 'campaign-creatives'] });
    },
  });
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
    () => deriveCampaignMetadata(campaignQuery.data),
    [campaignQuery.data],
  );

  const metricsSummary = useMemo(
    () => summarizeMetrics(campaignMetricsQuery.data?.report),
    [campaignMetricsQuery.data],
  );
  const trendSeries = useMemo(
    () => buildTrendSeries(campaignMetricsQuery.data?.report),
    [campaignMetricsQuery.data],
  );
  const productTable = useMemo(
    () => buildDimensionTable(productMetricsQuery.data?.report, 'product_id'),
    [productMetricsQuery.data],
  );
  const creatives = useMemo(
    () =>
      normalizeCreativesData(creativesQuery.data, creativeMetricsQuery.data, creativeHeatingQuery.data).sort(
        (a, b) => (b.metrics?.gmv || 0) - (a.metrics?.gmv || 0),
      ),
    [creativeHeatingQuery.data, creativeMetricsQuery.data, creativesQuery.data],
  );
  const seedCreatives = useMemo(
    () =>
      creatives
        .filter((creative) => isSeedCreative(creative))
        .sort((a, b) => (b.metrics?.roas || 0) - (a.metrics?.roas || 0)),
    [creatives],
  );
  const filteredCreatives = useMemo(() => {
    return creatives.filter((creative) => {
      if (!creativeStatusFilters.has(creative.status || 'UNKNOWN')) return false;
      if (showOnlyHeated && !isCreativeBoosting(creative)) return false;
      if (showSeedOnly && !isSeedCreative(creative)) return false;
      return true;
    });
  }, [creativeStatusFilters, creatives, showOnlyHeated, showSeedOnly]);

  const sortedCreatives = useMemo(() => {
    const list = [...filteredCreatives];
    const getValue = (creative) => {
      const metrics = creative.metrics || {};
      if (creativeSortKey === 'roas') return metrics.roas || 0;
      if (creativeSortKey === 'conversions') return metrics.orders || 0;
      return metrics.spend || 0;
    };
    return list.sort((a, b) => getValue(b) - getValue(a));
  }, [creativeSortKey, filteredCreatives]);

  const creativesByStatus = useMemo(() => {
    const groups = new Map();
    sortedCreatives.forEach((creative) => {
      const key = creative.status || 'UNKNOWN';
      if (!groups.has(key)) {
        groups.set(key, []);
      }
      groups.get(key).push(creative);
    });
    return creativeStatusOptions.map((option) => ({
      key: option.key,
      label: option.label,
      items: groups.get(option.key) || [],
    }));
  }, [creativeStatusOptions, sortedCreatives]);

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
  }, [campaignStoreId, identityList, productSelection, sessionId, storeIdFromQuery, updateProductsMutation]);

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
    await ensureFresh();
    syncMetricsMutation.mutate({ start_date: metricsParams.start_date, end_date: metricsParams.end_date });
  }, [ensureFresh, metricsParams.end_date, metricsParams.start_date, syncMetricsMutation]);

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

  const handleBoostCreative = useCallback(
    async (creative) => {
      const creativeId = creative?.creativeId || creative?.metadata?.id;
      if (!creativeId || creativeActionMutation.isPending) return;
      await ensureFresh();
      creativeActionMutation.mutate({ type: 'boost', creative_id: creativeId });
    },
    [creativeActionMutation, ensureFresh],
  );

  const handleStopHeat = useCallback(
    async (creative) => {
      const creativeId = creative?.creativeId || creative?.metadata?.id;
      if (!creativeId || creativeActionMutation.isPending) return;
      await ensureFresh();
      creativeActionMutation.mutate({ type: 'stop_heat', creative_id: creativeId });
    },
    [creativeActionMutation, ensureFresh],
  );

  const handlePause = useCallback(async () => {
    await ensureFresh();
    await applyActionMutation.mutateAsync({ type: 'pause' });
  }, [applyActionMutation, ensureFresh]);

  const handleResume = useCallback(async () => {
    await ensureFresh();
    await applyActionMutation.mutateAsync({ type: 'resume' });
  }, [applyActionMutation, ensureFresh]);

  const handleDelete = useCallback(() => {
    const confirmed = window.confirm('确定删除该系列？此操作无法恢复。');
    if (!confirmed) return;
    ensureFresh()
      .then(() => applyActionMutation.mutateAsync({ type: 'delete' }))
      .catch(() => {});
  }, [applyActionMutation, ensureFresh]);

  const openBudgetDialog = useCallback((mode) => {
    setBudgetDialog({ open: true, mode });
  }, []);

  const closeBudgetDialog = useCallback(() => {
    setBudgetDialog((prev) => ({ ...prev, open: false }));
  }, []);

  const handleBudgetSubmit = useCallback(
    (percent) => {
      const payload = {
        type: 'update_budget',
        payload: {
          direction: budgetDialog.mode === 'increase' ? 'increase' : 'decrease',
          percent,
        },
      };
      ensureFresh()
        .then(() => applyActionMutation.mutateAsync(payload))
        .catch(() => {});
      closeBudgetDialog();
    },
    [applyActionMutation, budgetDialog.mode, closeBudgetDialog, ensureFresh],
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
    setLastSaveMessage('');
    updateStrategyMutation.mutate(buildStrategyPayload(strategyDraft));
  }, [strategyDraft, updateStrategyMutation]);

  const handleStrategyPreview = useCallback(() => {
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
  }, [campaignMetadata, previewStrategyMutation, storeIdFromQuery, strategyDraft]);

  const latestPreviewResult = previewStrategyMutation.data;

  const statusLabel = determineStatusLabel(campaignMetadata.status);
  const spend = metricsSummary.spend || 0;
  const gmv = metricsSummary.gmv || 0;
  const roas = spend > 0 ? gmv / spend : null;

  const summaryCards = [
    { label: '花费', value: `$${formatMoney(spend)}` },
    { label: 'GMV', value: `$${formatMoney(gmv)}` },
    { label: '订单', value: formatNumber(metricsSummary.orders) },
    { label: 'ROAS', value: roas === null ? '—' : roas.toFixed(2) },
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
    : '创意维度数据需要广告系列和商品组配置。';

  return (
    <div className="gmvmax-campaign-detail">
      <header className="gmvmax-campaign-detail__header">
        <div className="gmvmax-campaign-detail__header-main">
          <h1>{campaignMetadata.name || `系列 ${campaignId}`}</h1>
          <span className={`gmvmax-status gmvmax-status--${statusLabel?.toLowerCase?.()}`}>
            {statusLabel}
          </span>
        </div>
        <div className="gmvmax-campaign-detail__info">
          <dl>
            <div>
              <dt>工作区</dt>
              <dd>{workspaceId}</dd>
            </div>
            <div>
              <dt>渠道</dt>
              <dd>{provider || '—'}</dd>
            </div>
            <div>
              <dt>账户</dt>
              <dd>{authId || '—'}</dd>
            </div>
            <div>
              <dt>广告主</dt>
              <dd>{campaignMetadata.advertiserName || advertiserId || '—'}</dd>
            </div>
            <div>
              <dt>店铺</dt>
              <dd>{campaignMetadata.storeName || storeIdFromQuery || '—'}</dd>
            </div>
            <div>
              <dt>商家中心</dt>
              <dd>{campaignMetadata.businessCenterName || '—'}</dd>
            </div>
          </dl>
          <div className="gmvmax-campaign-detail__products">
            <span>{campaignMetadata.products?.length || 0} 个商品</span>
            <div className="gmvmax-product-thumbnails">
              {campaignMetadata.products?.slice(0, 6).map((product) => (
                <div key={product.id || product.name} className="gmvmax-product-thumbnail">
                  {product.image ? <img src={product.image} alt={product.name || '商品'} /> : '📦'}
                </div>
              ))}
              {(campaignMetadata.products?.length || 0) > 6 ? (
                <span className="gmvmax-product-thumbnail gmvmax-product-thumbnail--more">
                  +{campaignMetadata.products.length - 6}
                </span>
              ) : null}
            </div>
          </div>
        </div>
        <div className="gmvmax-campaign-detail__actions">
          <button
            type="button"
            onClick={handlePause}
            disabled={applyActionMutation.isPending || isEnsuringFresh}
          >
            暂停
          </button>
          <button
            type="button"
            onClick={handleResume}
            disabled={applyActionMutation.isPending || isEnsuringFresh}
          >
            启用
          </button>
          <button
            type="button"
            onClick={handleDelete}
            disabled={applyActionMutation.isPending || isEnsuringFresh}
          >
            删除
          </button>
          <button
            type="button"
            onClick={() => openBudgetDialog('increase')}
            disabled={applyActionMutation.isPending || isEnsuringFresh}
          >
            提升预算
          </button>
          <button
            type="button"
            onClick={() => openBudgetDialog('decrease')}
            disabled={applyActionMutation.isPending || isEnsuringFresh}
          >
            降低预算
          </button>
          <button type="button" onClick={openActionLogs}>
            {GmvMaxTexts.actionLogs}
          </button>
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
      {notifications.error ? (
        <div className="gmvmax-error">{notifications.error.message || '通知刷新失败'}</div>
      ) : null}

      <section className="gmvmax-summary">
        <h2>{GmvMaxTexts.performanceSummary}</h2>
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
        <section className="gmvmax-automation">
          <div className="gmvmax-automation__header">
            <h2>策略自动化</h2>
            {strategyQuery.isFetching ? <Loading text="策略加载中…" /> : null}
            {strategyQuery.error ? (
              <div className="gmvmax-error">{strategyQuery.error.message || '策略加载失败'}</div>
            ) : null}
          </div>
          <div className="gmvmax-automation__summary">
            <div>
              <strong>策略状态：</strong>
              <span>{strategyHighlights.automation}</span>
              <span> · {strategyHighlights.autoHeating}</span>
            </div>
            <div className="gmvmax-automation__summary-details">
              <span>{strategyHighlights.heatingRule}</span>
              <span> · {strategyHighlights.pauseRule}</span>
            </div>
          </div>
          <div className="gmvmax-automation__grid">
            <div className="gmvmax-automation__column">
              <FormField label="启用自动化">
                <input
                  type="checkbox"
                  checked={Boolean(strategyDraft.enabled)}
                  onChange={(event) => handleStrategyFieldChange('enabled', event.target.checked)}
                />
              </FormField>
              <FormField label={<span title="达到触发条件后自动申请加热">自动加热</span>}>
                <input
                  type="checkbox"
                  checked={Boolean(strategyDraft.autoHeatingEnabled)}
                  onChange={(event) => handleStrategyFieldChange('autoHeatingEnabled', event.target.checked)}
                />
              </FormField>
              <FormField label="监测频率（分钟）">
                <input
                  type="number"
                  min={MIN_MONITORING_INTERVAL}
                  value={strategyDraft.cooldownMinutes}
                  onChange={(event) =>
                    handleStrategyFieldChange(
                      'cooldownMinutes',
                      Math.max(MIN_MONITORING_INTERVAL, Number(event.target.value) || MIN_MONITORING_INTERVAL),
                    )
                  }
                />
              </FormField>
              <FormField label="首次调整前的最短运行时间（分钟）">
                <input
                  type="number"
                  min="0"
                  value={strategyDraft.minRuntimeMinutes ?? ''}
                  onChange={(event) => handleStrategyFieldChange('minRuntimeMinutes', event.target.value)}
                />
              </FormField>
            </div>
            <div className="gmvmax-automation__column">
              <FormField label="目标 ROAS">
                <input
                  type="number"
                  step="0.01"
                  value={strategyDraft.thresholds.target_roi ?? ''}
                  onChange={(event) => handleThresholdChange('target_roi', event.target.value)}
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
              <FormField label={<span title="ROI 低于该值时自动暂停系列">自动暂停 ROI 阈值</span>}>
                <input
                  type="number"
                  step="0.01"
                  value={strategyDraft.thresholds.auto_pause_roi_threshold ?? ''}
                  onChange={(event) => handleThresholdChange('auto_pause_roi_threshold', event.target.value)}
                />
              </FormField>
              <FormField label="最高 ROAS">
                <input
                  type="number"
                  step="0.01"
                  value={strategyDraft.thresholds.max_roi ?? ''}
                  onChange={(event) => handleThresholdChange('max_roi', event.target.value)}
                />
              </FormField>
            </div>
            <div className="gmvmax-automation__column">
              <FormField label="每次评估最少曝光">
                <input
                  type="number"
                  min="0"
                  value={strategyDraft.thresholds.min_impressions ?? ''}
                  onChange={(event) => handleThresholdChange('min_impressions', event.target.value)}
                />
              </FormField>
              <FormField label={<span title="达到最少点击后才会评估是否加热">触发加热最少点击</span>}>
                <input
                  type="number"
                  min="0"
                  value={strategyDraft.thresholds.min_clicks ?? ''}
                  onChange={(event) => handleThresholdChange('min_clicks', event.target.value)}
                />
              </FormField>
              <FormField label="单日最高预算提升比例 (%)">
                <input
                  type="number"
                  min="0"
                  step="0.1"
                  value={strategyDraft.thresholds.max_budget_raise_pct_per_day ?? ''}
                  onChange={(event) => handleThresholdChange('max_budget_raise_pct_per_day', event.target.value)}
                />
              </FormField>
              <FormField label="单日最高预算降低比例 (%)">
                <input
                  type="number"
                  min="0"
                  step="0.1"
                  value={strategyDraft.thresholds.max_budget_cut_pct_per_day ?? ''}
                  onChange={(event) => handleThresholdChange('max_budget_cut_pct_per_day', event.target.value)}
                />
              </FormField>
              <FormField label="单次调整最大 ROAS 变化">
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={strategyDraft.thresholds.max_roas_step_per_adjust ?? ''}
                  onChange={(event) => handleThresholdChange('max_roas_step_per_adjust', event.target.value)}
                />
              </FormField>
            </div>
          </div>

          <section className="gmvmax-automation__rules">
            <div className="gmvmax-automation__rules-header">
              <div>
                <h3>高级规则（可选）</h3>
                <p className="gmvmax-automation__hint">基础阈值已生效，如需更复杂的逻辑可展开编辑。</p>
              </div>
              <button type="button" onClick={() => setShowAdvancedRules((prev) => !prev)}>
                {showAdvancedRules ? '收起' : '展开'}
              </button>
            </div>
            {showAdvancedRules ? (
              <div className="gmvmax-automation__rules-list">
                <div className="gmvmax-automation__rules-actions">
                  <button type="button" onClick={handleAddRule}>
                    新增规则
                  </button>
                </div>
                {(strategyDraft.rules || []).length === 0 ? (
                  <p>尚未配置规则，请添加以启动自动化。</p>
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
            ) : (
              <p>已隐藏高级规则，当前仅使用基础阈值配置。</p>
            )}
          </section>

          <div className="gmvmax-automation__footer">
            <div className="gmvmax-automation__footer-left">
              {lastSaveMessage ? <span>{lastSaveMessage}</span> : null}
            </div>
            <div className="gmvmax-automation__footer-actions">
              <button type="button" onClick={handleStrategyReset} disabled={!strategyDirty}>
                重置
              </button>
              <button
                type="button"
                onClick={handleStrategyPreview}
                disabled={previewStrategyMutation.isPending}
              >
                预览
              </button>
              <button
                type="button"
                className="primary"
                onClick={handleStrategySave}
                disabled={updateStrategyMutation.isPending || !strategyDirty}
              >
                {updateStrategyMutation.isPending ? '保存中…' : '保存配置'}
              </button>
            </div>
          </div>

          {previewStrategyMutation.error ? (
            <div className="gmvmax-error">
              {previewStrategyMutation.error.message || '预览失败'}
            </div>
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
            disabled={updateProductsMutation.isPending}
            onSelectAll={selectAllProducts}
            onClearAll={clearProducts}
            searchTerm={productSearch}
            onSearchChange={setProductSearch}
          />
          <div className="gmvmax-products__actions">
            <span>已选 {productSelection.size} 个商品</span>
            <button type="button" onClick={() => setProductSelection(new Set(initialProductSet))}>
              重置
            </button>
            <button
              type="button"
              className="primary"
              disabled={!productsChanged || updateProductsMutation.isPending}
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
            <div className="gmvmax-dashboard__range">
              {[{ key: 'today', label: '今日' }, { key: '7d', label: '近 7 日' }, { key: '30d', label: '近 30 日' }, { key: 'custom', label: '自定义' }].map((option) => (
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
                    value={customRange.start}
                    onChange={(event) => handleCustomRangeChange('start', event.target.value)}
                  />
                  <span>至</span>
                  <input
                    type="date"
                    value={customRange.end}
                    onChange={(event) => handleCustomRangeChange('end', event.target.value)}
                  />
                </div>
              ) : null}
            </div>
            <div className="gmvmax-dashboard__controls-right">
              {advertiserTimezone ? (
                <div className="gmvmax-dashboard__timezone">按广告主时区：{advertiserTimezone}</div>
              ) : null}
              <button
                type="button"
                className="gmvmax-button gmvmax-button--secondary"
                onClick={handleSyncMetrics}
                disabled={syncMetricsMutation.isPending}
              >
                {syncMetricsMutation.isPending ? '同步中…' : '同步数据'}
              </button>
            </div>
          </div>
          {campaignMetricsQuery.isFetching || productMetricsQuery.isFetching ? (
            <Loading text="数据加载中…" />
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
              <h3>消耗 / ROAS 趋势</h3>
              <span className="gmvmax-dashboard__chart-caption">按所选时间范围</span>
            </div>
            <TrendChart data={trendSeries} />
          </div>

          <div className="gmvmax-dashboard__table gmvmax-dashboard__table--creatives">
            {seedCreatives.length > 0 ? (
              <div className="gmvmax-seed-panel">
                <div className="gmvmax-seed-panel__header">
                  <div className="gmvmax-seed-panel__title">
                    <span role="img" aria-label="seed">⭐</span>
                    <span>种子视频 ({seedCreatives.length})</span>
                  </div>
                  <span className="gmvmax-seed-panel__hint" title={SEED_RULE_TEXT}>
                    规则：{SEED_RULE_TEXT}
                  </span>
                </div>
                <div className="gmvmax-seed-panel__grid">
                  {seedCreatives.slice(0, 6).map((creative) => (
                    <div key={creative.creativeId} className="gmvmax-seed-card">
                      <div className="gmvmax-seed-card__name" title={creative.name}>
                        ⭐ {creative.name}
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
                <label className="gmvmax-select gmvmax-select--inline">
                  <span>排序</span>
                  <select value={creativeSortKey} onChange={(event) => setCreativeSortKey(event.target.value)}>
                    <option value="spend">按消耗</option>
                    <option value="conversions">按转化</option>
                    <option value="roas">按 ROAS</option>
                  </select>
                </label>
              </div>
            </div>
            {creativesLoading ? <Loading text="创意加载中…" /> : null}
            {!creativeFiltersReady ? (
              <div className="gmvmax-text-muted">{missingCreativeFiltersMessage}</div>
            ) : creativesErrorMessage ? (
              <div
                className={
                  isMissingFilterError(creativesError) ? 'gmvmax-text-muted' : 'gmvmax-error'
                }
              >
                创意数据加载失败：{creativesErrorMessage}
              </div>
            ) : null}
            {creativeActionMutation.error ? (
              <div className="gmvmax-error">
                创意操作失败：{creativeActionMutation.error.message || '请稍后重试'}
              </div>
            ) : null}
            <div className="table-wrap">
              <table className="gmvmax-table gmvmax-creatives-table">
                <thead>
                  <tr>
                    <th>创意</th>
                    <th>状态</th>
                    <th>曝光</th>
                    <th>点击</th>
                    <th>转化</th>
                    <th>花费</th>
                    <th>GMV</th>
                    <th>ROAS</th>
                    <th>标签</th>
                    <th className="col-actions">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {creativesByStatus.map((group) => (
                    <Fragment key={group.key}>
                      <tr className="gmvmax-creatives__group-row">
                        <td colSpan={10}>{group.label}</td>
                      </tr>
                      {group.items.length === 0 ? (
                        <tr>
                          <td colSpan={10}>该状态暂无创意。</td>
                        </tr>
                      ) : null}
                      {group.items.map((creative) => {
                        const metrics = creative.metrics || {};
                        const boosting = isCreativeBoosting(creative);
                        const isSeed = isSeedCreative(creative);
                        return (
                          <tr key={creative.creativeId}>
                            <td>
                              <div className="gmvmax-creatives__name">
                                {creative.thumbnail ? (
                                  <img
                                    src={creative.thumbnail}
                                    alt={creative.name || '创意封面'}
                                    className="gmvmax-creatives__thumb"
                                  />
                                ) : (
                                  <span className="gmvmax-creatives__thumb gmvmax-creatives__thumb--placeholder">🎞️</span>
                                )}
                                <div>
                                  <div className="gmvmax-creatives__label">{creative.name}</div>
                                  <div className="gmvmax-creatives__id">ID: {creative.creativeId}</div>
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
                                {creativeStatusLabelMap.get(creative.status || 'UNKNOWN') || creative.status}
                              </span>
                            </td>
                            <td>{formatNumber(metrics.impressions)}</td>
                            <td>{formatNumber(metrics.clicks)}</td>
                            <td>{formatNumber(metrics.orders)}</td>
                            <td>${formatMoney(metrics.spend)}</td>
                            <td>${formatMoney(metrics.gmv)}</td>
                            <td>{metrics.roas ? metrics.roas.toFixed(2) : '—'}</td>
                            <td>
                              <div className="gmvmax-tag-list">
                                {boosting ? (
                                  <span className="gmvmax-tag gmvmax-tag--heat" title="加热中">
                                    🔥 加热中
                                  </span>
                                ) : null}
                                {isSeed ? (
                                  <span className="gmvmax-tag" title={SEED_RULE_TEXT}>
                                    ⭐ 种子
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
                                      disabled={creativeActionMutation.isPending}
                                    >
                                      停止加热
                                    </button>
                                  ) : (
                                    <button
                                      type="button"
                                      className="gmvmax-button gmvmax-button--ghost"
                                      onClick={() => handleBoostCreative(creative)}
                                      disabled={creativeActionMutation.isPending}
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
                  <th>商品 ID</th>
                  <th>花费</th>
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
                            <div>ID: {row.id}</div>
                            {row.name && row.name !== row.id ? (
                              <div className="gmvmax-text-muted">{row.name}</div>
                            ) : null}
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
