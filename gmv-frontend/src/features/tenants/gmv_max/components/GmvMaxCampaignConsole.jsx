import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  fetchGmvMaxCampaigns,
  fetchGmvMaxCampaignDetail,
  fetchGmvMaxStrategy,
  previewGmvMaxStrategy,
  fetchGmvMaxMetrics,
  applyGmvMaxAction,
  fetchGmvMaxActions,
  updateGmvMaxStrategy,
} from '../service.js';
import {
  GMV_MAX_STRATEGY_PRESETS,
  GMV_MAX_STRATEGY_PRESET_LIST,
} from '../strategyPresets.js';

export function GmvMaxCampaignConsole({ workspaceId, authId, advertiserId }) {
  const [selectedId, setSelectedId] = useState(null);
  const [selectedStoreId, setSelectedStoreId] = useState('ALL');
  const [metricsRange, setMetricsRange] = useState('7d');
  const [editingStrategyText, setEditingStrategyText] = useState('');
  const [strategyError, setStrategyError] = useState(null);
  const [selectedStrategyPreset, setSelectedStrategyPreset] = useState('balanced');
  const metricsRangeDates = useMemo(() => {
    const today = new Date();
    const end = new Date(
      Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()),
    );
    const start = new Date(end.getTime());
    const days = metricsRange === '30d' ? 30 : 7;
    start.setUTCDate(start.getUTCDate() - (days - 1));

    const toISODate = (date) =>
      `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(
        date.getUTCDate(),
      ).padStart(2, '0')}`;

    return {
      start: toISODate(start),
      end: toISODate(end),
    };
  }, [metricsRange]);
  const queryClient = useQueryClient();

  const campaignsQuery = useQuery({
    queryKey: ['gmvmax-campaigns', workspaceId, authId, advertiserId],
    enabled: Boolean(workspaceId && authId && advertiserId),
    queryFn: () =>
      fetchGmvMaxCampaigns({
        workspaceId,
        authId,
        advertiserId,
        sync: false,
      }),
  });

  const detailQuery = useQuery({
    queryKey: ['gmvmax-campaign-detail', workspaceId, authId, advertiserId, selectedId],
    enabled: Boolean(workspaceId && authId && advertiserId && selectedId),
    queryFn: () =>
      fetchGmvMaxCampaignDetail({
        workspaceId,
        authId,
        advertiserId,
        campaignId: selectedId,
        refresh: false,
      }),
  });

  const strategyQuery = useQuery({
    queryKey: ['gmvmax-strategy', workspaceId, authId, selectedId],
    enabled: Boolean(workspaceId && authId && selectedId),
    queryFn: () =>
      fetchGmvMaxStrategy({
        workspaceId,
        authId,
        campaignId: selectedId,
      }),
  });

  const previewQuery = useQuery({
    queryKey: ['gmvmax-strategy-preview', workspaceId, authId, selectedId],
    enabled: Boolean(workspaceId && authId && selectedId),
    queryFn: () =>
      previewGmvMaxStrategy({
        workspaceId,
        authId,
        campaignId: selectedId,
      }),
  });

  const metricsQuery = useQuery({
    queryKey: ['gmvmax-metrics', workspaceId, authId, advertiserId, selectedId, metricsRange],
    enabled: Boolean(workspaceId && authId && advertiserId && selectedId),
    queryFn: () =>
      fetchGmvMaxMetrics({
        workspaceId,
        authId,
        campaignId: selectedId,
        granularity: 'DAY',
        start: metricsRangeDates.start,
        end: metricsRangeDates.end,
      }),
  });

  const actionsQuery = useQuery({
    queryKey: ['gmvmax-actions', workspaceId, selectedId],
    enabled: Boolean(workspaceId && selectedId),
    queryFn: () =>
      fetchGmvMaxActions({
        workspaceId,
        campaignId: selectedId,
        limit: 50,
        offset: 0,
      }),
  });

  const actionMutation = useMutation({
    mutationFn: ({ action, payload, reason }) =>
      applyGmvMaxAction({
        workspaceId,
        authId,
        advertiserId,
        campaignId: selectedId,
        action,
        payload,
        reason,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-campaign-detail', workspaceId, authId, advertiserId, selectedId],
      });
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-strategy', workspaceId, authId, selectedId],
      });
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-strategy-preview', workspaceId, authId, selectedId],
      });
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-metrics', workspaceId, authId, advertiserId, selectedId],
      });
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-campaigns', workspaceId, authId, advertiserId],
      });
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-actions', workspaceId, selectedId],
      });
    },
  });

  function handleStart() {
    if (!selectedId) return;
    actionMutation.mutate({
      action: 'START',
      payload: {},
      reason: 'manual: start from console',
    });
  }

  function handlePause() {
    if (!selectedId) return;
    actionMutation.mutate({
      action: 'PAUSE',
      payload: {},
      reason: 'manual: pause from console',
    });
  }

  function handleSetBudget() {
    if (!selectedId) return;
    const input = window.prompt(
      '请输入新的日预算（单位：分，例：5000 表示 50.00）',
      '',
    );
    if (!input) return;
    const cents = Number.parseInt(input, 10);
    if (!Number.isFinite(cents) || cents <= 0) {
      window.alert('预算输入不合法');
      return;
    }
    actionMutation.mutate({
      action: 'SET_BUDGET',
      payload: { daily_budget_cents: cents },
      reason: 'manual: set budget from console',
    });
  }

  function handleSetRoas() {
    if (!selectedId) return;
    const input = window.prompt('请输入新的 ROAS 目标（例如 1.50）', '');
    if (!input) return;
    const value = String(input).trim();
    if (!value) {
      window.alert('ROAS 输入不能为空');
      return;
    }
    actionMutation.mutate({
      action: 'SET_ROAS',
      payload: { roas_bid: value },
      reason: 'manual: set roas from console',
    });
  }

  function buildMetricsSummary(data) {
    if (!data) return null;
    const rows =
      (Array.isArray(data?.items) && data.items) ||
      (Array.isArray(data?.list) && data.list) ||
      [];
    if (!rows.length) return null;

    let impressions = 0;
    let clicks = 0;
    let costCents = 0;
    let revenueCents = 0;
    let orders = 0;

    for (const row of rows) {
      impressions += row?.impressions || 0;
      clicks += row?.clicks || 0;
      costCents += row?.cost_cents || 0;
      revenueCents += row?.gross_revenue_cents || 0;
      orders += row?.orders || 0;
    }

    let roi = null;
    if (costCents > 0 && revenueCents > 0) {
      roi = revenueCents / costCents;
    }

    return {
      impressions,
      clicks,
      costCents,
      revenueCents,
      orders,
      roi,
    };
  }

  const metricsSummary = buildMetricsSummary(metricsQuery.data);

  useEffect(() => {
    if (!selectedId) {
      setEditingStrategyText('');
      setStrategyError(null);
      return;
    }
    if (strategyQuery.data) {
      try {
        setEditingStrategyText(JSON.stringify(strategyQuery.data, null, 2));
        setStrategyError(null);
      } catch (error) {
        setEditingStrategyText('');
      }
    } else {
      setEditingStrategyText('');
      setStrategyError(null);
    }
  }, [selectedId, strategyQuery.data]);

  if (!workspaceId || !authId) {
    return <div>请选择 workspace / 授权账号后查看 GMV Max Campaign。</div>;
  }

  if (!advertiserId) {
    return <div>请选择一个广告主以查看 GMV Max Campaign。</div>;
  }

  if (campaignsQuery.isLoading) {
    return <div>加载 GMV Max Campaign 列表中...</div>;
  }

  if (campaignsQuery.isError) {
    return <div>加载失败：{String(campaignsQuery.error)}</div>;
  }

  const list = Array.isArray(campaignsQuery.data?.items)
    ? campaignsQuery.data.items
    : campaignsQuery.data?.list || [];
  const storeStats = useMemo(() => {
    if (!Array.isArray(list) || list.length === 0) return [];
    const map = new Map();
    for (const item of list) {
      const storeId = item?.store_id || item?.shop_id || 'UNKNOWN';
      const storeName = item?.store_name || item?.shop_name || '未命名店铺';
      if (!map.has(storeId)) {
        map.set(storeId, {
          storeId,
          storeName,
          totalCampaigns: 0,
          activeCampaigns: 0,
          pausedCampaigns: 0,
        });
      }
      const stat = map.get(storeId);
      stat.totalCampaigns += 1;
      const status = item?.status || item?.campaign_status || '';
      const normalized = typeof status === 'string' ? status.toUpperCase() : '';
      if (normalized === 'ACTIVE') {
        stat.activeCampaigns += 1;
      } else if (normalized === 'PAUSED' || normalized === 'DISABLED') {
        stat.pausedCampaigns += 1;
      }
    }
    return Array.from(map.values());
  }, [list]);
  const visibleCampaigns = useMemo(() => {
    if (selectedStoreId === 'ALL') return list;
    return list.filter((item) => {
      const storeId = item?.store_id || item?.shop_id || 'UNKNOWN';
      return storeId === selectedStoreId;
    });
  }, [list, selectedStoreId]);

  useEffect(() => {
    if (!selectedId) return;
    if (selectedStoreId === 'ALL') return;
    const exists = visibleCampaigns.some((item) => {
      const id = item?.campaign_id || item?.id;
      return id === selectedId;
    });
    if (!exists) {
      setSelectedId(null);
    }
  }, [visibleCampaigns, selectedStoreId, selectedId]);

  const strategyMutation = useMutation({
    mutationFn: async () => {
      if (!selectedId) return;
      let parsed;
      try {
        parsed = editingStrategyText ? JSON.parse(editingStrategyText) : {};
      } catch (error) {
        setStrategyError(`JSON 解析失败，请检查格式：${String(error)}`);
        throw error;
      }
      setStrategyError(null);
      return updateGmvMaxStrategy({
        workspaceId,
        authId,
        campaignId: selectedId,
        data: parsed,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-strategy', workspaceId, authId, selectedId],
      });
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-strategy-preview', workspaceId, authId, selectedId],
      });
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-actions', workspaceId, selectedId],
      });
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-metrics', workspaceId, authId, advertiserId, selectedId],
      });
      queryClient.invalidateQueries({
        queryKey: ['gmvmax-campaigns', workspaceId, authId, advertiserId],
      });
    },
    onError: (error) => {
      if (!strategyError) {
        setStrategyError(`保存策略失败：${String(error)}`);
      }
    },
  });

  return (
    <div style={{ display: 'flex', gap: 16, marginTop: 24 }}>
      <div style={{ flex: '0 0 260px', borderRight: '1px solid #eee' }}>
        <h3>GMV Max Campaign 列表</h3>
        <div style={{ marginBottom: 8, fontSize: 12 }}>
          店铺筛选：
          <select
            value={selectedStoreId}
            onChange={(event) => setSelectedStoreId(event.target.value)}
            style={{ maxWidth: '100%' }}
          >
            <option value="ALL">全部店铺</option>
            {storeStats.map((store) => (
              <option key={store.storeId} value={store.storeId}>
                {store.storeName} ({store.totalCampaigns})
              </option>
            ))}
          </select>
        </div>
        {visibleCampaigns.length === 0 && <div>暂无 Campaign。</div>}
        <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
          {visibleCampaigns.map((item) => {
            const id = item?.campaign_id || item?.id;
            if (!id) return null;
            const name = item?.name || item?.campaign_name || id;
            const status = item?.status || item?.campaign_status || '';
            const isActive = typeof status === 'string' && status.toUpperCase() === 'ACTIVE';
            const isSelected = id === selectedId;
            return (
              <li
                key={id}
                onClick={() => setSelectedId(id)}
                style={{
                  padding: '4px 8px',
                  cursor: 'pointer',
                  backgroundColor: isSelected ? '#eef2ff' : 'transparent',
                }}
              >
                <div style={{ fontWeight: 600 }}>{name}</div>
                <div style={{ fontSize: 12, color: '#6b7280' }}>
                  {id} · {isActive ? 'ACTIVE' : status || ''}
                </div>
              </li>
            );
          })}
        </ul>
      </div>

      <div style={{ flex: 1 }}>
        <h3>详情 / 策略预览</h3>
        {selectedId && (
          <div style={{ marginBottom: 8, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button onClick={handleStart} disabled={actionMutation.isLoading}>
              启动
            </button>
            <button onClick={handlePause} disabled={actionMutation.isLoading}>
              暂停
            </button>
            <button onClick={handleSetBudget} disabled={actionMutation.isLoading}>
              改预算
            </button>
            <button onClick={handleSetRoas} disabled={actionMutation.isLoading}>
              改 ROAS
            </button>
            {actionMutation.isLoading && (
              <span style={{ fontSize: 12 }}>执行中...</span>
            )}
          </div>
        )}
        {!selectedId && <div>请在左侧选择一个 Campaign</div>}
        {selectedId && (
          <div style={{ display: 'grid', gap: 12 }}>
            <section>
              <h4>最近指标概览（Day 粒度汇总）</h4>
              <div style={{ marginBottom: 8, fontSize: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                <span>时间范围：</span>
                <button
                  type="button"
                  onClick={() => setMetricsRange('7d')}
                  disabled={metricsRange === '7d'}
                >
                  近 7 天
                </button>
                <button
                  type="button"
                  onClick={() => setMetricsRange('30d')}
                  disabled={metricsRange === '30d'}
                >
                  近 30 天
                </button>
                <span style={{ color: '#6b7280' }}>
                  ({metricsRangeDates.start} ~ {metricsRangeDates.end})
                </span>
              </div>
              {metricsQuery.isLoading && <div>加载指标中...</div>}
              {metricsQuery.isError && (
                <div>加载失败：{String(metricsQuery.error)}</div>
              )}
              {metricsSummary && (
                <div style={{ fontSize: 13, lineHeight: 1.6 }}>
                  <div>曝光: {metricsSummary.impressions}</div>
                  <div>点击: {metricsSummary.clicks}</div>
                  <div>花费 (cents): {metricsSummary.costCents}</div>
                  <div>GMV (cents): {metricsSummary.revenueCents}</div>
                  <div>订单: {metricsSummary.orders}</div>
                  <div>
                    ROI:{' '}
                    {metricsSummary.roi != null
                      ? Number.isFinite(metricsSummary.roi)
                        ? metricsSummary.roi.toFixed(4)
                        : metricsSummary.roi
                      : '-'}
                  </div>
                </div>
              )}
              {!metricsSummary &&
                !metricsQuery.isLoading &&
                !metricsQuery.isError && <div>暂无指标数据。</div>}
            </section>

            <section>
              <h4>Campaign 详情</h4>
              {detailQuery.isLoading && <div>加载详情中...</div>}
              {detailQuery.isError && <div>加载失败：{String(detailQuery.error)}</div>}
              {detailQuery.data && (
                <pre style={{ maxHeight: 240, overflow: 'auto', fontSize: 12 }}>
                  {JSON.stringify(detailQuery.data, null, 2)}
                </pre>
              )}
            </section>

            <section>
              <h4>策略配置（JSON 编辑）</h4>
              {strategyQuery.isLoading && <div>加载策略中...</div>}
              {strategyQuery.isError && <div>加载失败：{String(strategyQuery.error)}</div>}
              {strategyError && (
                <div style={{ color: 'red', fontSize: 12, marginBottom: 4 }}>{strategyError}</div>
              )}
              <div style={{ marginBottom: 8, fontSize: 12, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                <span>策略模板：</span>
                <select
                  value={selectedStrategyPreset}
                  onChange={(event) => setSelectedStrategyPreset(event.target.value)}
                  style={{ maxWidth: 240 }}
                >
                  {GMV_MAX_STRATEGY_PRESET_LIST.map((preset) => (
                    <option key={preset.key} value={preset.key}>
                      {preset.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => {
                    const preset = GMV_MAX_STRATEGY_PRESETS[selectedStrategyPreset];
                    if (!preset) return;
                    try {
                      setEditingStrategyText(JSON.stringify(preset, null, 2));
                      setStrategyError(null);
                    } catch (error) {
                      setStrategyError(`应用模板失败：${String(error)}`);
                    }
                  }}
                >
                  应用模板
                </button>
                <span style={{ color: '#6b7280' }}>
                  {
                    GMV_MAX_STRATEGY_PRESET_LIST.find(
                      (preset) => preset.key === selectedStrategyPreset,
                    )?.description || ''
                  }
                </span>
              </div>
              <div style={{ marginBottom: 8, fontSize: 12 }}>
                <p style={{ margin: 0 }}>说明：此处展示并可编辑当前策略 JSON，可选择模板快速填充。</p>
              </div>
              <textarea
                value={editingStrategyText}
                onChange={(event) => setEditingStrategyText(event.target.value)}
                style={{
                  width: '100%',
                  minHeight: 180,
                  fontFamily: 'monospace',
                  fontSize: 12,
                  boxSizing: 'border-box',
                }}
              />
              <div style={{ marginTop: 8, fontSize: 12 }}>
                <button
                  type="button"
                  onClick={() => strategyMutation.mutate()}
                  disabled={strategyMutation.isLoading || !selectedId}
                >
                  保存策略
                </button>
                <button
                  type="button"
                  style={{ marginLeft: 8 }}
                  disabled={strategyMutation.isLoading || strategyQuery.isLoading}
                  onClick={() => {
                    if (strategyQuery.data) {
                      try {
                        setEditingStrategyText(JSON.stringify(strategyQuery.data, null, 2));
                        setStrategyError(null);
                      } catch (error) {
                        setStrategyError(`重置失败：${String(error)}`);
                      }
                    }
                  }}
                >
                  重置为当前策略
                </button>
                {strategyMutation.isLoading && <span style={{ marginLeft: 8 }}>保存中...</span>}
              </div>
            </section>

            <section>
              <h4>策略预览（只读）</h4>
              {previewQuery.isLoading && <div>计算预览中...</div>}
              {previewQuery.isError && <div>加载失败：{String(previewQuery.error)}</div>}
              {previewQuery.data && (
                <pre style={{ maxHeight: 200, overflow: 'auto', fontSize: 12 }}>
                  {JSON.stringify(previewQuery.data, null, 2)}
                </pre>
              )}
            </section>

            <section>
              <h4>操作日志（最近 50 条）</h4>
              {actionsQuery.isLoading && <div>加载日志中...</div>}
              {actionsQuery.isError && (
                <div>加载失败：{String(actionsQuery.error)}</div>
              )}
              {!actionsQuery.isLoading &&
                !actionsQuery.isError && (
                  (() => {
                    const data = actionsQuery.data || {};
                    const items =
                      (Array.isArray(data?.items) && data.items) ||
                      (Array.isArray(data?.list) && data.list) ||
                      [];
                    if (!items.length) {
                      return <div>暂无日志。</div>;
                    }
                    return (
                      <table
                        style={{
                          width: '100%',
                          borderCollapse: 'collapse',
                          fontSize: 12,
                        }}
                      >
                        <thead>
                          <tr>
                            <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>时间</th>
                            <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>动作</th>
                            <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>结果</th>
                            <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>原因</th>
                            <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>执行者</th>
                          </tr>
                        </thead>
                        <tbody>
                          {items.map((log, index) => (
                            <tr key={log?.id ?? index}>
                              <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                                {log?.created_at || log?.createdAt || ''}
                              </td>
                              <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                                {log?.action || ''}
                              </td>
                              <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                                {log?.result || ''}
                              </td>
                              <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                                {log?.reason || log?.error_message || ''}
                              </td>
                              <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                                {log?.performed_by || log?.actor || ''}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    );
                  })()
                )}
            </section>

            <section>
              <h4>多店铺对比（当前广告主）</h4>
              {campaignsQuery.isLoading && <div>加载 Campaign 列表中...</div>}
              {campaignsQuery.isError && <div>加载失败：{String(campaignsQuery.error)}</div>}
              {!campaignsQuery.isLoading &&
                !campaignsQuery.isError &&
                (storeStats.length === 0 ? (
                  <div>暂无店铺数据。</div>
                ) : (
                  <table
                    style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}
                  >
                    <thead>
                      <tr>
                        <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>店铺名称</th>
                        <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>店铺 ID</th>
                        <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>Campaign 总数</th>
                        <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>ACTIVE</th>
                        <th style={{ borderBottom: '1px solid #ddd', padding: 4 }}>PAUSED/DISABLED</th>
                      </tr>
                    </thead>
                    <tbody>
                      {storeStats.map((store) => (
                        <tr key={store.storeId}>
                          <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                            {store.storeName}
                          </td>
                          <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                            {store.storeId}
                          </td>
                          <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                            {store.totalCampaigns}
                          </td>
                          <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                            {store.activeCampaigns}
                          </td>
                          <td style={{ borderBottom: '1px solid #f0f0f0', padding: 4 }}>
                            {store.pausedCampaigns}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ))}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}
