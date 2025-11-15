import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';

import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';
import Modal from '@/components/ui/Modal.jsx';

import {
  useApplyGmvMaxActionMutation,
  useGmvMaxActionLogsQuery,
  useGmvMaxCampaignQuery,
  useGmvMaxMetricsQuery,
  useGmvMaxStrategyQuery,
  usePreviewGmvMaxStrategyMutation,
  useSyncGmvMaxMetricsMutation,
  useUpdateGmvMaxStrategyMutation,
} from '../hooks/gmvMaxQueries.js';

const MIN_MONITORING_INTERVAL = 10;
const METRIC_CHOICES = [
  { value: 'roi', label: 'ROAS' },
  { value: 'spend', label: 'Spend' },
  { value: 'gmv', label: 'GMV' },
  { value: 'orders', label: 'Orders' },
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
  { value: 'pause', label: 'Pause campaign' },
  { value: 'resume', label: 'Resume campaign' },
  { value: 'increase_budget', label: 'Increase budget %' },
  { value: 'decrease_budget', label: 'Decrease budget %' },
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
  return {
    enabled: Boolean(data.enabled ?? true),
    cooldownMinutes: data.cooldown_minutes ?? data.cooldownMinutes ?? 30,
    minRuntimeMinutes:
      data.min_runtime_minutes_before_first_change ??
      data.minRuntimeMinutesBeforeFirstChange ??
      120,
    thresholds: {
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
    },
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
  if (!status) return 'Unknown';
  const normalized = String(status).toUpperCase();
  if (normalized.includes('PAUSE') || normalized.includes('DISABLE')) return 'Paused';
  if (normalized.includes('ENABLE') || normalized.includes('RUN') || normalized.includes('OK'))
    return 'Running';
  if (normalized.includes('ARCHIVE')) return 'Archived';
  return status;
}

function TrendChart({ data }) {
  if (!data || data.length === 0) {
    return <div className="gmvmax-chart gmvmax-chart--empty">No metrics available.</div>;
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
        <FormField label="Metric">
          <select value={rule.metric} onChange={handleChange('metric')}>
            {METRIC_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Operator">
          <select value={rule.operator} onChange={handleChange('operator')}>
            {OPERATOR_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Value">
          <input type="number" value={rule.value} onChange={handleChange('value')} />
        </FormField>
      </div>
      <div className="gmvmax-rule__row">
        <FormField label="Conjunction">
          <select value={rule.conjunction} onChange={handleChange('conjunction')}>
            <option value="AND">AND</option>
            <option value="OR">OR</option>
          </select>
        </FormField>
        <FormField label="Metric (optional)">
          <select value={rule.secondaryMetric} onChange={handleChange('secondaryMetric')}>
            <option value="">—</option>
            {METRIC_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Operator">
          <select value={rule.secondaryOperator} onChange={handleChange('secondaryOperator')}>
            {OPERATOR_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Value">
          <input type="number" value={rule.secondaryValue} onChange={handleChange('secondaryValue')} />
        </FormField>
      </div>
      <div className="gmvmax-rule__row">
        <FormField label="Action">
          <select value={rule.action} onChange={handleChange('action')}>
            {ACTION_CHOICES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Action Value">
          <input type="number" value={rule.actionValue} onChange={handleChange('actionValue')} />
        </FormField>
        <button type="button" className="gmvmax-rule__remove" onClick={onRemove}>
          Remove Rule
        </button>
      </div>
    </div>
  );
}

function ActionLogsList({ logs, isLoading, error, onRetry }) {
  if (isLoading) {
    return <Loading text="Loading action logs…" />;
  }
  if (error) {
    return (
      <div className="gmvmax-error">
        <span>{error.message || 'Failed to load action logs'}</span>
        <button type="button" onClick={onRetry} className="gmvmax-error__retry">
          Retry
        </button>
      </div>
    );
  }
  const entries = ensureArray(logs?.entries);
  if (entries.length === 0) {
    return <p>No action logs yet.</p>;
  }
  return (
    <ul className="gmvmax-action-logs">
      {entries.map((entry) => (
        <li key={entry.id || `${entry.type}-${entry.created_at}`}> 
          <div className="gmvmax-action-logs__header">
            <strong>{entry.type}</strong>
            <span>{entry.created_at || entry.timestamp}</span>
          </div>
          <pre>{JSON.stringify(entry.payload || entry.details || entry, null, 2)}</pre>
        </li>
      ))}
    </ul>
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

  const title = mode === 'increase' ? 'Increase budget' : 'Decrease budget';

  return (
    <Modal open={open} title={title} onClose={onClose}>
      <form className="gmvmax-budget-dialog" onSubmit={handleSubmit}>
        <FormField label="Percentage">
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
            Cancel
          </button>
          <button type="submit" className="primary">
            Apply
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

  useEffect(() => {
    const tab = searchParams.get('tab') === 'dashboard' ? 'dashboard' : 'automation';
    setActiveTab(tab);
  }, [searchParams]);

  const metricsParams = useMemo(() => computeTimeRange(timeRange, customRange), [customRange, timeRange]);

  const commonEnabled = Boolean(workspaceId && provider && authId && campaignId);

  const campaignQuery = useGmvMaxCampaignQuery(workspaceId, provider, authId, campaignId, {
    enabled: commonEnabled,
  });

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

  const strategyQuery = useGmvMaxStrategyQuery(workspaceId, provider, authId, campaignId, {
    enabled: commonEnabled,
  });

  const actionLogsQuery = useGmvMaxActionLogsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    { limit: 50 },
    {
      enabled: commonEnabled && actionLogsOpen,
    },
  );

  const syncMetricsMutation = useSyncGmvMaxMetricsMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => metricsQuery.refetch(),
  });
  const applyActionMutation = useApplyGmvMaxActionMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      campaignQuery.refetch();
      metricsQuery.refetch();
      if (actionLogsOpen) {
        actionLogsQuery.refetch();
      }
    },
  });
  const updateStrategyMutation = useUpdateGmvMaxStrategyMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: () => {
      setStrategyDirty(false);
      setLastSaveMessage('Strategy saved successfully.');
      strategyQuery.refetch();
    },
    onError: (error) => {
      setLastSaveMessage(error?.message || 'Failed to save strategy.');
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
  const creativesTable = useMemo(
    () => buildDimensionTable(metricsQuery.data?.report, 'creative'),
    [metricsQuery.data],
  );
  const productTable = useMemo(
    () => buildDimensionTable(metricsQuery.data?.report, 'product'),
    [metricsQuery.data],
  );

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

  const handlePause = useCallback(() => {
    applyActionMutation.mutate({ type: 'pause' });
  }, [applyActionMutation]);

  const handleResume = useCallback(() => {
    applyActionMutation.mutate({ type: 'resume' });
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
    { label: 'Spend', value: `$${formatMoney(spend)}` },
    { label: 'GMV', value: `$${formatMoney(gmv)}` },
    { label: 'Orders', value: formatNumber(metricsSummary.orders) },
    { label: 'ROAS', value: roas === null ? '—' : roas.toFixed(2) },
    { label: 'CTR', value: formatPercent(average(metricsSummary.ctrValues)) },
    { label: 'CPC', value: `$${formatMoney(average(metricsSummary.cpcValues))}` },
    { label: 'CPM', value: `$${formatMoney(average(metricsSummary.cpmValues))}` },
  ];

  return (
    <div className="gmvmax-campaign-detail">
      <header className="gmvmax-campaign-detail__header">
        <div className="gmvmax-campaign-detail__header-main">
          <h1>{campaignMetadata.name || `Campaign ${campaignId}`}</h1>
          <span className={`gmvmax-status gmvmax-status--${statusLabel?.toLowerCase?.()}`}>
            {statusLabel}
          </span>
        </div>
        <div className="gmvmax-campaign-detail__info">
          <dl>
            <div>
              <dt>Workspace</dt>
              <dd>{workspaceId}</dd>
            </div>
            <div>
              <dt>Provider</dt>
              <dd>{provider || '—'}</dd>
            </div>
            <div>
              <dt>Account</dt>
              <dd>{authId || '—'}</dd>
            </div>
            <div>
              <dt>Advertiser</dt>
              <dd>{campaignMetadata.advertiserName || advertiserId || '—'}</dd>
            </div>
            <div>
              <dt>Store</dt>
              <dd>{campaignMetadata.storeName || storeIdFromQuery || '—'}</dd>
            </div>
            <div>
              <dt>Business Center</dt>
              <dd>{campaignMetadata.businessCenterName || '—'}</dd>
            </div>
          </dl>
          <div className="gmvmax-campaign-detail__products">
            <span>{campaignMetadata.products?.length || 0} products</span>
            <div className="gmvmax-product-thumbnails">
              {campaignMetadata.products?.slice(0, 6).map((product) => (
                <div key={product.id || product.name} className="gmvmax-product-thumbnail">
                  {product.image ? <img src={product.image} alt={product.name || 'Product'} /> : '📦'}
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
            Pause
          </button>
          <button type="button" onClick={handleResume} disabled={applyActionMutation.isPending}>
            Resume
          </button>
          <button type="button" onClick={() => openBudgetDialog('increase')} disabled={applyActionMutation.isPending}>
            Increase Budget
          </button>
          <button type="button" onClick={() => openBudgetDialog('decrease')} disabled={applyActionMutation.isPending}>
            Decrease Budget
          </button>
          <button type="button" onClick={openActionLogs}>
            View Action Logs
          </button>
        </div>
        {applyActionMutation.error ? (
          <div className="gmvmax-error">{applyActionMutation.error.message || 'Action failed'}</div>
        ) : null}
      </header>

      <section className="gmvmax-summary">
        <h2>Performance Summary</h2>
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
          Automation
        </button>
        <button
          type="button"
          className={activeTab === 'dashboard' ? 'active' : ''}
          onClick={() => handleTabChange('dashboard')}
        >
          Dashboard
        </button>
      </div>

      {activeTab === 'automation' ? (
        <section className="gmvmax-automation">
          <div className="gmvmax-automation__header">
            <h2>Strategy Automation</h2>
            {strategyQuery.isFetching ? <Loading text="Loading strategy…" /> : null}
            {strategyQuery.error ? (
              <div className="gmvmax-error">{strategyQuery.error.message || 'Failed to load strategy'}</div>
            ) : null}
          </div>
          <div className="gmvmax-automation__grid">
            <div className="gmvmax-automation__column">
              <FormField label="Automation enabled">
                <input
                  type="checkbox"
                  checked={Boolean(strategyDraft.enabled)}
                  onChange={(event) => handleStrategyFieldChange('enabled', event.target.checked)}
                />
              </FormField>
              <FormField label="Monitoring interval (minutes)">
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
              <FormField label="Min runtime before first change (minutes)">
                <input
                  type="number"
                  min="0"
                  value={strategyDraft.minRuntimeMinutes ?? ''}
                  onChange={(event) => handleStrategyFieldChange('minRuntimeMinutes', event.target.value)}
                />
              </FormField>
            </div>
            <div className="gmvmax-automation__column">
              <FormField label="Target ROAS">
                <input
                  type="number"
                  step="0.01"
                  value={strategyDraft.thresholds.target_roi ?? ''}
                  onChange={(event) => handleThresholdChange('target_roi', event.target.value)}
                />
              </FormField>
              <FormField label="Min ROAS">
                <input
                  type="number"
                  step="0.01"
                  value={strategyDraft.thresholds.min_roi ?? ''}
                  onChange={(event) => handleThresholdChange('min_roi', event.target.value)}
                />
              </FormField>
              <FormField label="Max ROAS">
                <input
                  type="number"
                  step="0.01"
                  value={strategyDraft.thresholds.max_roi ?? ''}
                  onChange={(event) => handleThresholdChange('max_roi', event.target.value)}
                />
              </FormField>
            </div>
            <div className="gmvmax-automation__column">
              <FormField label="Min impressions per evaluation">
                <input
                  type="number"
                  min="0"
                  value={strategyDraft.thresholds.min_impressions ?? ''}
                  onChange={(event) => handleThresholdChange('min_impressions', event.target.value)}
                />
              </FormField>
              <FormField label="Min clicks per evaluation">
                <input
                  type="number"
                  min="0"
                  value={strategyDraft.thresholds.min_clicks ?? ''}
                  onChange={(event) => handleThresholdChange('min_clicks', event.target.value)}
                />
              </FormField>
              <FormField label="Max budget raise % per day">
                <input
                  type="number"
                  min="0"
                  step="0.1"
                  value={strategyDraft.thresholds.max_budget_raise_pct_per_day ?? ''}
                  onChange={(event) => handleThresholdChange('max_budget_raise_pct_per_day', event.target.value)}
                />
              </FormField>
              <FormField label="Max budget cut % per day">
                <input
                  type="number"
                  min="0"
                  step="0.1"
                  value={strategyDraft.thresholds.max_budget_cut_pct_per_day ?? ''}
                  onChange={(event) => handleThresholdChange('max_budget_cut_pct_per_day', event.target.value)}
                />
              </FormField>
              <FormField label="Max ROAS step per adjust">
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
              <h3>Automation Rules</h3>
              <button type="button" onClick={handleAddRule}>
                Add Rule
              </button>
            </div>
            {(strategyDraft.rules || []).length === 0 ? (
              <p>No rules configured. Add one to start automating.</p>
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
          </section>

          <div className="gmvmax-automation__footer">
            <div className="gmvmax-automation__footer-left">
              {lastSaveMessage ? <span>{lastSaveMessage}</span> : null}
            </div>
            <div className="gmvmax-automation__footer-actions">
              <button type="button" onClick={handleStrategyReset} disabled={!strategyDirty}>
                Reset
              </button>
              <button
                type="button"
                onClick={handleStrategyPreview}
                disabled={previewStrategyMutation.isPending}
              >
                Preview
              </button>
              <button
                type="button"
                className="primary"
                onClick={handleStrategySave}
                disabled={updateStrategyMutation.isPending || !strategyDirty}
              >
                Save
              </button>
            </div>
          </div>

          {previewStrategyMutation.error ? (
            <div className="gmvmax-error">
              {previewStrategyMutation.error.message || 'Preview failed'}
            </div>
          ) : null}
          {latestPreviewResult ? (
            <div className="gmvmax-preview-result">
              <h4>Preview Result</h4>
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
                7 days
              </label>
              <label>
                <input
                  type="radio"
                  name="gmvmax-range"
                  value="30d"
                  checked={timeRange === '30d'}
                  onChange={() => handleTimeRangeChange('30d')}
                />
                30 days
              </label>
              <label>
                <input
                  type="radio"
                  name="gmvmax-range"
                  value="custom"
                  checked={timeRange === 'custom'}
                  onChange={() => handleTimeRangeChange('custom')}
                />
                Custom
              </label>
              {timeRange === 'custom' ? (
                <div className="gmvmax-dashboard__custom-range">
                  <input
                    type="date"
                    value={customRange.start}
                    onChange={(event) => handleCustomRangeChange('start', event.target.value)}
                  />
                  <span>to</span>
                  <input
                    type="date"
                    value={customRange.end}
                    onChange={(event) => handleCustomRangeChange('end', event.target.value)}
                  />
                </div>
              ) : null}
            </div>
            <button type="button" onClick={handleSyncMetrics} disabled={syncMetricsMutation.isPending}>
              Sync Metrics
            </button>
            {metricsQuery.isFetching ? <Loading text="Loading metrics…" /> : null}
            {metricsQuery.error ? (
              <div className="gmvmax-error">{metricsQuery.error.message || 'Metrics failed to load'}</div>
            ) : null}
          </div>

          <div className="gmvmax-dashboard__chart">
            <TrendChart data={trendSeries} />
          </div>

          <div className="gmvmax-dashboard__tables">
            <div className="gmvmax-dashboard__table">
              <h3>Creatives</h3>
              <table>
                <thead>
                  <tr>
                    <th>Creative</th>
                    <th>Spend</th>
                    <th>GMV</th>
                    <th>Orders</th>
                    <th>CTR</th>
                    <th>CPC</th>
                  </tr>
                </thead>
                <tbody>
                  {creativesTable.length === 0 ? (
                    <tr>
                      <td colSpan={6}>No creative metrics.</td>
                    </tr>
                  ) : (
                    creativesTable
                      .slice()
                      .sort((a, b) => b.gmv - a.gmv)
                      .map((row) => (
                        <tr key={row.id}>
                          <td>{row.name}</td>
                          <td>${formatMoney(row.spend)}</td>
                          <td>${formatMoney(row.gmv)}</td>
                          <td>{formatNumber(row.orders)}</td>
                          <td>{formatPercent(row.ctr)}</td>
                          <td>${formatMoney(row.cpc)}</td>
                        </tr>
                      ))
                  )}
                </tbody>
              </table>
            </div>
            <div className="gmvmax-dashboard__table">
              <h3>Products</h3>
              <table>
                <thead>
                  <tr>
                    <th>Product</th>
                    <th>Spend</th>
                    <th>GMV</th>
                    <th>Orders</th>
                    <th>Clicks</th>
                    <th>ROAS</th>
                  </tr>
                </thead>
                <tbody>
                  {productTable.length === 0 ? (
                    <tr>
                      <td colSpan={6}>No product metrics.</td>
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

      <Modal open={actionLogsOpen} title="Campaign Action Logs" onClose={closeActionLogs}>
        <ActionLogsList
          logs={actionLogsQuery.data}
          isLoading={actionLogsQuery.isFetching}
          error={actionLogsQuery.error}
          onRetry={() => actionLogsQuery.refetch()}
        />
      </Modal>
    </div>
  );
}
