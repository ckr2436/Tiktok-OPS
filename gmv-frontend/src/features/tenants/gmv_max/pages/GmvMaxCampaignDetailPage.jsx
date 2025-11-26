import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';

import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';
import Modal from '@/components/ui/Modal.jsx';

import {
  useApplyGmvMaxActionMutation,
  useGmvMaxCampaignCreativesQuery,
  useGmvMaxCampaignQuery,
  useGmvMaxCreativeHeatingQuery,
  useGmvMaxCreativeMetricsQuery,
  useGmvMaxMetricsQuery,
  useGmvMaxStrategyQuery,
  usePreviewGmvMaxStrategyMutation,
  useSyncGmvMaxMetricsMutation,
  useUpdateGmvMaxStrategyMutation,
} from '../hooks/gmvMaxQueries.js';
import useGmvMaxNotifications from '../hooks/useGmvMaxNotifications.js';
import { GmvMaxTexts } from '../locale.js';
import ActionLogsTable from '../components/ActionLogsTable.jsx';

const MIN_MONITORING_INTERVAL = 10;
const METRIC_CHOICES = [
  { value: 'roi', label: 'ROAS' },
  { value: 'spend', label: '消耗' },
  { value: 'gmv', label: 'GMV' },
  { value: 'orders', label: '订单' },
  { value: 'ctr', label: 'CTR' },
  { value: 'cpc', label: 'CPC' },
];
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
  const spend = Number(metrics.spend ?? metrics.cost ?? metrics.net_cost ?? 0) || 0;
  const gmv = Number(metrics.gmv ?? metrics.gross_revenue ?? metrics.revenue ?? 0) || 0;
  const clicks = Number(metrics.clicks ?? metrics.total_clicks ?? 0) || 0;
  const impressions = Number(metrics.impressions ?? metrics.views ?? 0) || 0;
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
  const value =
    metrics[key] ??
    metrics[key?.toUpperCase?.()] ??
    metrics[key?.toLowerCase?.()] ??
    metrics[`total_${key}`] ??
    metrics[`total${key?.charAt(0)?.toUpperCase?.()}${key?.slice(1)}`];
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

function computeTimeRange(range, customRange) {
  if (range === 'custom' && customRange?.start && customRange?.end) {
    return { start_date: customRange.start, end_date: customRange.end };
  }
  const today = new Date();
  const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const start = new Date(end);
  const days = range === '30d' ? 29 : 6;
  start.setUTCDate(start.getUTCDate() - days);
  const format = (date) =>
    `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(
      date.getUTCDate(),
    ).padStart(2, '0')}`;
  return { start_date: format(start), end_date: format(end) };
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
      gmv: getMetricValue(entry, 'gmv'),
    }))
    .filter((item) => item.label);
}

function buildDimensionTable(report, dimensionKey, extraKeys = []) {
  const entries = ensureArray(report?.list);
  const groups = new Map();
  for (const entry of entries) {
    const dimensions = entry.dimensions || entry.dimension || {};
    const key = dimensions[dimensionKey] || dimensions[`${dimensionKey}_id`] || 'unknown';
    const name =
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
      item?.creative_id || item?.creativeId || item?.id || item?.code || item?.creative?.id || item?.creativeCode;
    if (!creativeId) continue;
    const row = ensureRow(creativeId);
    row.name =
      item?.creative_name || item?.creativeName || item?.name || item?.label || item?.title || row.name || creativeId;
    row.thumbnail = item?.thumbnail || item?.image || item?.cover_url || item?.coverUrl || row.thumbnail;
    row.status = normalizeCreativeStatus(item?.creative_status || item?.status || item?.state || row.status);
    row.metrics = parseCreativeMetrics(item?.metrics || row.metrics);
    row.metadata = { ...row.metadata, ...item };
  }

  const metricsItems = metricsData?.items || metricsData?.results;
  if (Array.isArray(metricsItems)) {
    for (const entry of metricsItems) {
      const creativeId =
        entry?.creative_id || entry?.creativeId || entry?.id || entry?.code || entry?.metrics?.creative_id;
      if (!creativeId) continue;
      const row = ensureRow(creativeId);
      row.name =
        entry?.creative_name || entry?.creativeName || entry?.name || entry?.label || entry?.title || row.name || creativeId;
      row.status = normalizeCreativeStatus(entry?.creative_status || entry?.status || entry?.state || row.status);
      row.metrics = parseCreativeMetrics(entry?.metrics || entry);
      row.metadata = { ...row.metadata, ...entry };
    }
  }

  if (metricsData?.report) {
    for (const entry of ensureArray(metricsData.report?.list)) {
      const dimensions = entry.dimensions || entry.dimension || {};
      const creativeId =
        dimensions.creative || dimensions.creative_id || dimensions.creativeId || dimensions.id || dimensions.code;
      if (!creativeId) continue;
      const row = ensureRow(creativeId);
      row.name =
        dimensions.creative_name ||
        dimensions.creativeName ||
        dimensions.name ||
        dimensions.title ||
        row.name ||
        creativeId;
      row.status = normalizeCreativeStatus(dimensions.creative_status || dimensions.status || row.status);
      row.metrics = parseCreativeMetrics(entry.metrics || entry);
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
    ...data.map((point) => Math.max(point.spend || 0, point.gmv || 0)),
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
  const gmvPath = buildPath('gmv');
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
      <path d={gmvPath} className="gmvmax-chart__line gmvmax-chart__line--gmv" />
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

  const [activeTab, setActiveTab] = useState(() =>
    searchParams.get('tab') === 'dashboard' ? 'dashboard' : 'automation',
  );
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
  const queryClient = useQueryClient();

  useEffect(() => {
    const tab = searchParams.get('tab') === 'dashboard' ? 'dashboard' : 'automation';
    setActiveTab(tab);
  }, [searchParams]);

  const metricsParams = useMemo(() => computeTimeRange(timeRange, customRange), [customRange, timeRange]);

  const commonEnabled = Boolean(workspaceId && provider && authId && campaignId);

  const campaignQuery = useGmvMaxCampaignQuery(workspaceId, provider, authId, campaignId, {
    enabled: commonEnabled,
  });

  const isDashboardTab = activeTab === 'dashboard';

  const metricsQuery = useGmvMaxMetricsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    { ...metricsParams, advertiser_id: advertiserId || undefined },
    {
      enabled: commonEnabled,
    },
  );

  const creativesQuery = useGmvMaxCampaignCreativesQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    undefined,
    {
      enabled: commonEnabled && isDashboardTab,
    },
  );

  const creativeMetricsQuery = useGmvMaxCreativeMetricsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    undefined,
    {
      enabled: commonEnabled && isDashboardTab,
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
    onSuccess: () => metricsQuery.refetch(),
  });
  const applyActionMutation = useApplyGmvMaxActionMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      campaignQuery.refetch();
      metricsQuery.refetch();
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

  const metricsSummary = useMemo(() => summarizeMetrics(metricsQuery.data?.report), [
    metricsQuery.data,
  ]);
  const trendSeries = useMemo(
    () => buildTrendSeries(metricsQuery.data?.report),
    [metricsQuery.data],
  );
  const productTable = useMemo(
    () => buildDimensionTable(metricsQuery.data?.report, 'product'),
    [metricsQuery.data],
  );
  const creatives = useMemo(
    () =>
      normalizeCreativesData(creativesQuery.data, creativeMetricsQuery.data, creativeHeatingQuery.data).sort(
        (a, b) => (b.metrics?.gmv || 0) - (a.metrics?.gmv || 0),
      ),
    [creativeHeatingQuery.data, creativeMetricsQuery.data, creativesQuery.data],
  );
  const creativeGroups = useMemo(() => {
    const inQueue = [];
    const learning = [];
    const delivering = [];
    const others = [];
    for (const creative of creatives) {
      if (creative.status === 'IN_QUEUE') {
        inQueue.push(creative);
        continue;
      }
      if (creative.status === 'LEARNING') {
        learning.push(creative);
        continue;
      }
      if (creative.status === 'DELIVERING') {
        delivering.push(creative);
        continue;
      }
      others.push(creative);
    }
    return { inQueue, learning, delivering, others };
  }, [creatives]);
  const creativeSections = useMemo(
    () => [
      { key: 'delivering', title: `投放中 (${creativeGroups.delivering.length})`, items: creativeGroups.delivering },
      { key: 'learning', title: `学习中 (${creativeGroups.learning.length})`, items: creativeGroups.learning },
      { key: 'inQueue', title: `排队中 (${creativeGroups.inQueue.length})`, items: creativeGroups.inQueue },
      { key: 'others', title: `其他 (${creativeGroups.others.length})`, items: creativeGroups.others },
    ],
    [creativeGroups],
  );
  const creativesLoading =
    creativesQuery.isLoading || creativeMetricsQuery.isLoading || creativeHeatingQuery.isLoading;
  const creativesError = creativesQuery.error || creativeMetricsQuery.error || creativeHeatingQuery.error;

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

  const handleSyncMetrics = useCallback(() => {
    syncMetricsMutation.mutate({ start_date: metricsParams.start_date, end_date: metricsParams.end_date });
  }, [metricsParams.end_date, metricsParams.start_date, syncMetricsMutation]);

  const handleBoostCreative = useCallback(
    (creative) => {
      const creativeId = creative?.creativeId || creative?.metadata?.id;
      if (!creativeId || creativeActionMutation.isPending) return;
      creativeActionMutation.mutate({ type: 'boost', creative_id: creativeId });
    },
    [creativeActionMutation],
  );

  const handleStopHeat = useCallback(
    (creative) => {
      const creativeId = creative?.creativeId || creative?.metadata?.id;
      if (!creativeId || creativeActionMutation.isPending) return;
      creativeActionMutation.mutate({ type: 'stop_heat', creative_id: creativeId });
    },
    [creativeActionMutation],
  );

  const handlePause = useCallback(() => {
    applyActionMutation.mutate({ type: 'pause' });
  }, [applyActionMutation]);

  const handleResume = useCallback(() => {
    applyActionMutation.mutate({ type: 'resume' });
  }, [applyActionMutation]);

  const handleDelete = useCallback(() => {
    const confirmed = window.confirm('确定删除该系列？此操作无法恢复。');
    if (!confirmed) return;
    applyActionMutation.mutate({ type: 'delete' });
  }, [applyActionMutation]);

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
      applyActionMutation.mutate(payload);
      closeBudgetDialog();
    },
    [applyActionMutation, budgetDialog.mode, closeBudgetDialog],
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

  const average = (values) => {
    if (!values || values.length === 0) return null;
    return values.reduce((sum, value) => sum + value, 0) / values.length;
  };

  const summaryCards = [
    { label: '花费', value: `$${formatMoney(spend)}` },
    { label: 'GMV', value: `$${formatMoney(gmv)}` },
    { label: '订单', value: formatNumber(metricsSummary.orders) },
    { label: 'ROAS', value: roas === null ? '—' : roas.toFixed(2) },
    { label: 'CTR', value: formatPercent(average(metricsSummary.ctrValues)) },
    { label: 'CPC', value: `$${formatMoney(average(metricsSummary.cpcValues))}` },
    { label: 'CPM', value: `$${formatMoney(average(metricsSummary.cpmValues))}` },
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
          <button type="button" onClick={handlePause} disabled={applyActionMutation.isPending}>
            暂停
          </button>
          <button type="button" onClick={handleResume} disabled={applyActionMutation.isPending}>
            启用
          </button>
          <button type="button" onClick={handleDelete} disabled={applyActionMutation.isPending}>
            删除
          </button>
          <button type="button" onClick={() => openBudgetDialog('increase')} disabled={applyActionMutation.isPending}>
            提升预算
          </button>
          <button type="button" onClick={() => openBudgetDialog('decrease')} disabled={applyActionMutation.isPending}>
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
      ) : (
        <section className="gmvmax-dashboard">
          <div className="gmvmax-dashboard__controls">
            <div className="gmvmax-dashboard__range">
              <label>
                <input
                  type="radio"
                  name="gmvmax-range"
                  value="7d"
                  checked={timeRange === '7d'}
                  onChange={() => handleTimeRangeChange('7d')}
                />
                7 天
              </label>
              <label>
                <input
                  type="radio"
                  name="gmvmax-range"
                  value="30d"
                  checked={timeRange === '30d'}
                  onChange={() => handleTimeRangeChange('30d')}
                />
                30 天
              </label>
              <label>
                <input
                  type="radio"
                  name="gmvmax-range"
                  value="custom"
                  checked={timeRange === 'custom'}
                  onChange={() => handleTimeRangeChange('custom')}
                />
                自定义
              </label>
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
            <button type="button" onClick={handleSyncMetrics} disabled={syncMetricsMutation.isPending}>
              同步数据
            </button>
            {metricsQuery.isFetching ? <Loading text="数据加载中…" /> : null}
            {metricsQuery.error ? (
              <div className="gmvmax-error">{metricsQuery.error.message || '数据加载失败'}</div>
            ) : null}
          </div>

          <div className="gmvmax-dashboard__chart">
            <TrendChart data={trendSeries} />
          </div>

          <div className="gmvmax-dashboard__tables">
            <div className="gmvmax-dashboard__table gmvmax-dashboard__table--creatives">
              <h3>{GmvMaxTexts.creatives}</h3>
              {creativesLoading ? <Loading text="创意加载中…" /> : null}
              {creativesError ? (
                <div className="gmvmax-error">创意数据加载失败：{creativesError.message || '未知错误'}</div>
              ) : null}
              {creativeActionMutation.error ? (
                <div className="gmvmax-error">
                  创意操作失败：{creativeActionMutation.error.message || '请稍后重试'}
                </div>
              ) : null}
              {creativeSections.map((group) => (
                <details key={group.key} open>
                  <summary>{group.title}</summary>
                  {group.items.length === 0 ? (
                    <p>暂无对应创意。</p>
                  ) : (
                    <div className="gmvmax-creatives__table-wrapper">
                      <table className="gmvmax-creatives__table">
                        <thead>
                          <tr>
                            <th>创意</th>
                            <th>曝光</th>
                            <th>点击</th>
                            <th>点击率</th>
                            <th>花费</th>
                            <th>GMV</th>
                            <th>ROI</th>
                            <th>订单</th>
                            <th>加热</th>
                            <th>操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          {group.items.map((creative) => {
                            const metrics = creative.metrics || {};
                            const boosting = isCreativeBoosting(creative);
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
                                      <div className="gmvmax-creatives__badge gmvmax-creatives__badge--muted">
                                        状态：{creative.status}
                                      </div>
                                    </div>
                                  </div>
                                </td>
                                <td>{formatNumber(metrics.impressions)}</td>
                                <td>{formatNumber(metrics.clicks)}</td>
                                <td>{formatPercent(metrics.ctr)}</td>
                                <td>${formatMoney(metrics.spend)}</td>
                                <td>${formatMoney(metrics.gmv)}</td>
                                <td>{metrics.roas ? metrics.roas.toFixed(2) : '—'}</td>
                                <td>{formatNumber(metrics.orders)}</td>
                                <td>{boosting ? '🔥' : '—'}</td>
                                <td>
                                  {creative.status === 'DELIVERING' ? (
                                    boosting ? (
                                      <button
                                        type="button"
                                        onClick={() => handleStopHeat(creative)}
                                        disabled={creativeActionMutation.isPending}
                                      >
                                        停止加热
                                      </button>
                                    ) : (
                                      <button
                                        type="button"
                                        onClick={() => handleBoostCreative(creative)}
                                        disabled={creativeActionMutation.isPending}
                                      >
                                        加热
                                      </button>
                                    )
                                  ) : (
                                    <span className="gmvmax-creatives__badge gmvmax-creatives__badge--muted">无操作</span>
                                  )}
                                  {resolveLastEvaluated(creative) ? (
                                    <div className="gmvmax-creatives__meta">
                                      最近更新：{new Date(resolveLastEvaluated(creative)).toLocaleString()}
                                    </div>
                                  ) : null}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </details>
              ))}
            </div>
            <div className="gmvmax-dashboard__table">
              <h3>{GmvMaxTexts.products}</h3>
              <table>
                <thead>
                  <tr>
                    <th>商品</th>
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
                            <td>{row.name}</td>
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
          </div>
        </section>
      )}

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
