import { useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { useQueryClient } from '@tanstack/react-query';

import Loading from '@/components/ui/Loading.jsx';

import {
  useGmvMaxCampaignCreativesQuery,
  useGmvMaxCreativeHeatingQuery,
  useGmvMaxCreativeMetricsQuery,
  useStartGmvMaxCreativeHeatingMutation,
  useStopGmvMaxCreativeHeatingMutation,
} from '../hooks/gmvMaxQueries.js';

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

function parseCreativeMetrics(metrics) {
  if (!metrics || typeof metrics !== 'object') {
    return {
      impressions: 0,
      clicks: 0,
      spend: 0,
      cost: 0,
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
  const cost = spend;
  return { impressions, clicks, spend, cost, gmv, orders, ctr, cpc, roas };
}

function ensureArray(value) {
  if (Array.isArray(value)) return value;
  if (value === undefined || value === null) return [];
  return [value];
}

function buildLegacyCreativeRows(report) {
  const entries = ensureArray(report?.list);
  const groups = new Map();
  for (const entry of entries) {
    const dimensions = entry.dimensions || entry.dimension || {};
    const creativeId =
      dimensions.creative ||
      dimensions.creative_id ||
      dimensions.creativeId ||
      dimensions.id ||
      dimensions.code ||
      'unknown';
    const creativeName =
      dimensions.creative_name ||
      dimensions.creativeName ||
      dimensions.name ||
      dimensions.title ||
      String(creativeId);
    if (!groups.has(creativeId)) {
      groups.set(creativeId, {
        creativeId,
        creativeName,
        metrics: {
          impressions: 0,
          clicks: 0,
          spend: 0,
          cost: 0,
          gmv: 0,
          orders: 0,
          ctr: 0,
          cpc: 0,
          roas: 0,
        },
      });
    }
    const group = groups.get(creativeId);
    const metrics = parseCreativeMetrics(entry.metrics || entry);
    group.metrics.impressions += metrics.impressions;
    group.metrics.clicks += metrics.clicks;
    group.metrics.spend += metrics.spend;
    group.metrics.cost += metrics.cost;
    group.metrics.gmv += metrics.gmv;
    group.metrics.orders += metrics.orders;
    group.metrics.ctr += metrics.ctr;
    group.metrics.cpc += metrics.cpc;
    group.metrics.roas += metrics.roas;
  }
  return Array.from(groups.values());
}

function normalizeCreativesData(creativesData, metricsData, heatingData) {
  const rows = new Map();

  const baseItems = ensureArray(creativesData?.items ?? creativesData?.list ?? creativesData);
  for (const item of baseItems) {
    const creativeId =
      item?.creative_id ||
      item?.creativeId ||
      item?.id ||
      item?.code ||
      item?.creative?.id ||
      item?.creativeCode;
    if (!creativeId) continue;
    rows.set(String(creativeId), {
      creativeId: String(creativeId),
      creativeName:
        item?.creative_name || item?.creativeName || item?.name || item?.label || item?.title || String(creativeId),
      metrics: parseCreativeMetrics(item?.metrics),
      heating: item?.heating || null,
      metadata: item,
    });
  }

  const metricsItems = metricsData?.items || metricsData?.results;
  if (Array.isArray(metricsItems)) {
    for (const entry of metricsItems) {
      const creativeId =
        entry?.creative_id || entry?.creativeId || entry?.id || entry?.code || entry?.metrics?.creative_id;
      if (!creativeId) continue;
      if (!rows.has(String(creativeId))) {
        rows.set(String(creativeId), {
          creativeId: String(creativeId),
          creativeName:
            entry?.creative_name ||
            entry?.creativeName ||
            entry?.name ||
            entry?.label ||
            entry?.title ||
            String(creativeId),
          metrics: parseCreativeMetrics(entry?.metrics || entry),
          heating: entry?.heating || null,
          metadata: entry,
        });
      } else {
        const target = rows.get(String(creativeId));
        target.creativeName =
          entry?.creative_name || entry?.creativeName || entry?.name || entry?.label || target.creativeName;
        target.metrics = parseCreativeMetrics(entry?.metrics || entry);
        if (!target.heating && entry?.heating) {
          target.heating = entry.heating;
        }
        target.metadata = { ...target.metadata, ...entry };
      }
    }
  }

  if (metricsData?.report) {
    for (const row of buildLegacyCreativeRows(metricsData.report)) {
      if (!rows.has(row.creativeId)) {
        rows.set(row.creativeId, {
          creativeId: row.creativeId,
          creativeName: row.creativeName,
          metrics: row.metrics,
          heating: null,
          metadata: {},
        });
      } else {
        const target = rows.get(row.creativeId);
        target.metrics = row.metrics;
        if (!target.creativeName) {
          target.creativeName = row.creativeName;
        }
      }
    }
  }

  const heatingItems = ensureArray(heatingData?.items ?? heatingData?.list ?? heatingData);
  for (const entry of heatingItems) {
    const creativeId =
      entry?.creative_id || entry?.creativeId || entry?.id || entry?.code || entry?.creative?.id || entry?.creativeId;
    if (!creativeId) continue;
    if (!rows.has(String(creativeId))) {
      rows.set(String(creativeId), {
        creativeId: String(creativeId),
        creativeName:
          entry?.creative_name || entry?.creativeName || entry?.name || entry?.label || entry?.title || String(creativeId),
        metrics: parseCreativeMetrics(entry?.metrics),
        heating: entry,
        metadata: entry,
      });
    } else {
      const target = rows.get(String(creativeId));
      target.heating = entry;
      if (!target.creativeName) {
        target.creativeName =
          entry?.creative_name || entry?.creativeName || entry?.name || entry?.label || entry?.title || target.creativeName;
      }
      target.metadata = { ...target.metadata, ...entry };
    }
  }

  return Array.from(rows.values());
}

function resolveHeatingStatus(heating) {
  if (!heating) return 'IDLE';
  if (heating.is_heating_active || heating.isHeatingActive) return 'HEATING';
  return heating.status || heating.state || 'IDLE';
}

function resolveLastEvaluated(heating) {
  return heating?.last_evaluated_at || heating?.lastEvaluatedAt || heating?.updated_at || heating?.updatedAt || null;
}

function buildStartPayload(creative) {
  const heating = creative.heating || {};
  const payload = {};
  if (heating.mode) payload.mode = heating.mode;
  if (heating.currency) payload.currency = heating.currency;
  if (heating.max_duration_minutes ?? heating.maxDurationMinutes) {
    payload.max_duration_minutes = heating.max_duration_minutes ?? heating.maxDurationMinutes;
  }
  if (heating.note) payload.note = heating.note;
  if (heating.target_daily_budget ?? heating.targetDailyBudget) {
    payload.target_daily_budget = heating.target_daily_budget ?? heating.targetDailyBudget;
  } else if (heating.budget_delta ?? heating.budgetDelta ?? heating.default_budget_delta) {
    payload.budget_delta =
      heating.budget_delta ?? heating.budgetDelta ?? heating.default_budget_delta ?? heating.defaultBudgetDelta;
  } else {
    payload.budget_delta = creative.metrics?.spend ? Math.max(creative.metrics.spend * 0.1, 1) : 1;
  }
  if (!payload.currency && heating.currency) {
    payload.currency = heating.currency;
  }
  if (!payload.currency && creative.metadata?.currency) {
    payload.currency = creative.metadata.currency;
  }
  if (!payload.mode) {
    payload.mode = heating.mode || 'MANUAL';
  }
  return payload;
}

function GmvMaxCreativesPanel({ workspaceId, provider, authId, campaignId }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState('all');

  const creativesQuery = useGmvMaxCampaignCreativesQuery(workspaceId, provider, authId, campaignId, undefined, {
    enabled: Boolean(workspaceId && provider && authId && campaignId),
  });
  const creativeMetricsQuery = useGmvMaxCreativeMetricsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    undefined,
    {
      enabled: Boolean(workspaceId && provider && authId && campaignId),
    },
  );
  const creativeHeatingQuery = useGmvMaxCreativeHeatingQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    undefined,
    {
      enabled: Boolean(workspaceId && provider && authId && campaignId),
    },
  );

  const sharedSuccess = () => {
    queryClient.invalidateQueries({ queryKey: ['gmvMax', 'creative-heating'] });
    queryClient.invalidateQueries({ queryKey: ['gmvMax', 'creative-metrics'] });
    queryClient.invalidateQueries({ queryKey: ['gmvMax', 'campaign-creatives'] });
  };
  const sharedError = (error) => {
    const message = error?.message || error?.response?.data?.message || 'Failed to update creative heating.';
    if (typeof window !== 'undefined' && typeof window.alert === 'function') {
      window.alert(message);
    } else {
      console.error(message);
    }
  };

  const startMutation = useStartGmvMaxCreativeHeatingMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: sharedSuccess,
    onError: sharedError,
  });
  const stopMutation = useStopGmvMaxCreativeHeatingMutation(workspaceId, provider, authId, campaignId, {
    onSuccess: sharedSuccess,
    onError: sharedError,
  });

  const creatives = useMemo(
    () =>
      normalizeCreativesData(
        creativesQuery.data,
        creativeMetricsQuery.data,
        creativeHeatingQuery.data,
      ).sort((a, b) => (b.metrics?.gmv || 0) - (a.metrics?.gmv || 0)),
    [creativesQuery.data, creativeMetricsQuery.data, creativeHeatingQuery.data],
  );

  const filteredCreatives = useMemo(() => {
    if (filter === 'heating') {
      return creatives.filter((item) => resolveHeatingStatus(item.heating) === 'HEATING');
    }
    if (filter === 'idle') {
      return creatives.filter((item) => resolveHeatingStatus(item.heating) !== 'HEATING');
    }
    return creatives;
  }, [creatives, filter]);

  const isLoading =
    creativesQuery.isLoading || creativeMetricsQuery.isLoading || creativeHeatingQuery.isLoading;
  const hasError = creativesQuery.error || creativeMetricsQuery.error || creativeHeatingQuery.error;

  const handleStart = (creative) => {
    if (startMutation.isPending || stopMutation.isPending) return;
    const payload = buildStartPayload(creative);
    startMutation.mutate({ creativeId: creative.creativeId, payload });
  };

  const handleStop = (creative) => {
    if (startMutation.isPending || stopMutation.isPending) return;
    stopMutation.mutate({ creativeId: creative.creativeId, payload: {} });
  };

  return (
    <section className="gmvmax-creatives">
      <header className="gmvmax-creatives__header">
        <div>
          <h3>Creatives</h3>
          <p>Creative-level performance with heating status.</p>
        </div>
        <div className="gmvmax-creatives__filters">
          <label>
            <input
              type="radio"
              name="gmvmax-creatives-filter"
              value="all"
              checked={filter === 'all'}
              onChange={() => setFilter('all')}
            />
            All
          </label>
          <label>
            <input
              type="radio"
              name="gmvmax-creatives-filter"
              value="heating"
              checked={filter === 'heating'}
              onChange={() => setFilter('heating')}
            />
            Heating
          </label>
          <label>
            <input
              type="radio"
              name="gmvmax-creatives-filter"
              value="idle"
              checked={filter === 'idle'}
              onChange={() => setFilter('idle')}
            />
            Idle
          </label>
        </div>
      </header>

      {isLoading ? <Loading text="Loading creatives…" /> : null}
      {hasError ? (
        <div className="gmvmax-error">
          Failed to load creatives. {creativesQuery.error?.message || creativeMetricsQuery.error?.message || creativeHeatingQuery.error?.message}
        </div>
      ) : null}

      <div className="gmvmax-creatives__table-wrapper">
        <table className="gmvmax-creatives__table">
          <thead>
            <tr>
              <th>Creative</th>
              <th>Impressions</th>
              <th>Clicks</th>
              <th>Spend</th>
              <th>GMV</th>
              <th>Orders</th>
              <th>CTR</th>
              <th>CPC</th>
              <th>ROAS</th>
              <th>Heating status</th>
              <th>Last evaluation</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {filteredCreatives.length === 0 ? (
              <tr>
                <td colSpan={12}>No creatives found for this campaign.</td>
              </tr>
            ) : (
              filteredCreatives.map((creative) => {
                const status = resolveHeatingStatus(creative.heating);
                const lastEvaluated = resolveLastEvaluated(creative.heating);
                const metrics = creative.metrics || {};
                const isActive = status === 'HEATING';
                return (
                  <tr key={creative.creativeId}>
                    <td>
                      <div className="gmvmax-creatives__name">
                        <span className="gmvmax-creatives__label">{creative.creativeName}</span>
                        <span className="gmvmax-creatives__id">{creative.creativeId}</span>
                      </div>
                    </td>
                    <td>{formatNumber(metrics.impressions)}</td>
                    <td>{formatNumber(metrics.clicks)}</td>
                    <td>${formatMoney(metrics.spend)}</td>
                    <td>${formatMoney(metrics.gmv)}</td>
                    <td>{formatNumber(metrics.orders)}</td>
                    <td>{formatPercent(metrics.ctr)}</td>
                    <td>${formatMoney(metrics.cpc)}</td>
                    <td>{metrics.roas ? metrics.roas.toFixed(2) : '—'}</td>
                    <td>
                      <span className={`gmvmax-creatives__status gmvmax-creatives__status--${status.toLowerCase()}`}>
                        {status}
                      </span>
                      {creative.heating?.auto_stop_enabled === false || creative.heating?.autoStopEnabled === false ? (
                        <span className="gmvmax-creatives__badge">Auto-stop disabled</span>
                      ) : null}
                      {creative.heating?.last_evaluation_result || creative.heating?.lastEvaluationResult ? (
                        <span className="gmvmax-creatives__badge gmvmax-creatives__badge--muted">
                          {creative.heating?.last_evaluation_result || creative.heating?.lastEvaluationResult}
                        </span>
                      ) : null}
                    </td>
                    <td>{lastEvaluated ? new Date(lastEvaluated).toLocaleString() : '—'}</td>
                    <td>
                      <div className="gmvmax-creatives__actions">
                        <button
                          type="button"
                          className="primary"
                          onClick={() => handleStart(creative)}
                          disabled={isActive || startMutation.isPending || stopMutation.isPending}
                        >
                          Start heating
                        </button>
                        <button
                          type="button"
                          onClick={() => handleStop(creative)}
                          disabled={!isActive || startMutation.isPending || stopMutation.isPending}
                        >
                          Stop heating
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

GmvMaxCreativesPanel.propTypes = {
  workspaceId: PropTypes.oneOfType([PropTypes.string, PropTypes.number]).isRequired,
  provider: PropTypes.string.isRequired,
  authId: PropTypes.oneOfType([PropTypes.string, PropTypes.number]).isRequired,
  campaignId: PropTypes.oneOfType([PropTypes.string, PropTypes.number]).isRequired,
};

export default GmvMaxCreativesPanel;
