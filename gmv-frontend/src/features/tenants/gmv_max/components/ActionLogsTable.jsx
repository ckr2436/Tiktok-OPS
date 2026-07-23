import { useEffect, useMemo, useState } from 'react';

import Loading from '@/components/ui/Loading.jsx';

import { useGmvMaxActionLogsQuery } from '../hooks/gmvMaxQueries.js';
import { GmvMaxTexts } from '../locale.js';

function ensureArray(value) {
  if (Array.isArray(value)) return value;
  if (!value) return [];
  return [value];
}

function resolveTimestamp(entry) {
  return (
    entry?.timestamp ||
    entry?.created_at ||
    entry?.createdAt ||
    entry?.time ||
    entry?.ts ||
    null
  );
}

function formatTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

const VALUE_LABELS = {
  status: '状态',
  daily_budget_cents: '每日预算',
  roas_bid: 'ROAS 出价',
  spend: '消耗',
  gmv: 'GMV',
  orders: '订单',
  roas: 'ROAS',
  result: '结果',
};

function formatLogValue(value) {
  if (value === undefined || value === null) return '-';
  if (typeof value === 'object') {
    const entries = Object.entries(value).filter(([, item]) => item !== undefined && item !== null);
    if (!entries.length) return '-';
    return entries
      .map(([key, item]) => `${VALUE_LABELS[key] || key}：${formatLogValue(item)}`)
      .join('，');
  }
  if (typeof value === 'number') return Number(value).toLocaleString();
  return String(value);
}

function formatActionLabel(type) {
  switch ((type || '').toUpperCase()) {
    case 'HEAT':
    case 'BOOST':
    case 'BOOST_CREATIVE':
      return '加热素材';
    case 'STOP_HEAT':
    case 'STOP':
      return '停止加热';
    case 'ADD':
    case 'ADD_BACK_CREATIVE':
      return '恢复素材';
    case 'REMOVE':
    case 'REMOVE_CREATIVE':
      return '排除素材';
    case 'HOLD':
      return '保持当前状态';
    case 'PAUSE':
      return '暂停系列';
    case 'START':
    case 'ENABLE':
    case 'RESUME':
      return '启用系列';
    case 'REBUILD':
    case 'RESET_CAMPAIGN':
      return '重建系列';
    case 'INCREASE_BUDGET':
    case 'RAISE_BUDGET':
      return '提升预算';
    case 'DECREASE_BUDGET':
      return '降低预算';
    default:
      return type || '-';
  }
}

function describeTarget(entry) {
  if (entry?.creative_id || entry?.creativeId) {
    return `素材 ${entry.creative_id || entry.creativeId}`;
  }
  if (entry?.campaign_id || entry?.campaignId) {
    return `系列 ${entry.campaign_id || entry.campaignId}`;
  }
  return '当前系列';
}

function formatReason(reason) {
  const reasonLabels = {
    'creative_guard:scheduled_retest': '到达复测时间，恢复素材采样',
    'creative_guard:roi_below_target': '素材 ROAS 低于动态目标',
    'creative_guard:no_order_spend_threshold': '素材无订单消耗达到动态阈值',
    'creative_guard:manual_add': '人工恢复素材',
    'creative_guard:manual_remove': '人工排除素材',
  };
  return reasonLabels[reason] || reason || '';
}

function buildDescription(entry) {
  const pieces = [];
  const reason = formatReason(entry?.reason || entry?.comment || entry?.note);
  const result = String(entry?.result || '').toUpperCase();
  if (reason) pieces.push(reason);
  if (result === 'FAILED' && entry?.error_message) pieces.push(`失败：${entry.error_message}`);
  if (result === 'SKIPPED') pieces.push('本轮仅观察，未执行变更');
  return pieces.join('；') || '-';
}

export default function ActionLogsTable({
  workspaceId,
  provider,
  authId,
  campaignId,
  params = {},
  pageSize = 20,
}) {
  const [page, setPage] = useState(1);
  const paramsKey = JSON.stringify(params || {});

  useEffect(() => {
    setPage(1);
  }, [authId, campaignId, pageSize, provider, paramsKey, workspaceId]);

  const queryParams = useMemo(
    () => ({
      sort: '-timestamp',
      ...params,
      page,
      page_size: pageSize,
    }),
    [page, pageSize, params],
  );

  const logsQuery = useGmvMaxActionLogsQuery(workspaceId, provider, authId, campaignId, queryParams, {
    enabled: Boolean(workspaceId && provider && authId && campaignId),
    keepPreviousData: true,
  });

  const entries = useMemo(() => {
    const rawEntries = ensureArray(logsQuery.data?.entries || logsQuery.data?.items);
    return rawEntries
      .map((entry) => ({ ...entry, resolved_timestamp: resolveTimestamp(entry) }))
      .sort((a, b) => {
        const timeA = a.resolved_timestamp ? new Date(a.resolved_timestamp).getTime() : 0;
        const timeB = b.resolved_timestamp ? new Date(b.resolved_timestamp).getTime() : 0;
        return timeB - timeA;
      });
  }, [logsQuery.data]);
  const total = Math.max(entries.length, Number(logsQuery.data?.total) || 0);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  if (logsQuery.isLoading) return <Loading text="操作日志加载中..." />;

  if (logsQuery.error) {
    return (
      <div className="gmvmax-error">
        <span>{logsQuery.error.message || '操作日志暂时无法加载'}</span>
        <button type="button" onClick={() => logsQuery.refetch()} className="gmvmax-error__retry">
          {GmvMaxTexts.retry}
        </button>
      </div>
    );
  }

  if (!entries.length && total === 0) return <p>暂无操作记录。</p>;

  return (
    <div className="gmvmax-action-logs">
      <div className="gmvmax-action-logs__header">
        <h3>{GmvMaxTexts.campaignActionLogs}</h3>
        <div>
          <span>共 {total} 条</span>
          <button type="button" onClick={() => logsQuery.refetch()} disabled={logsQuery.isFetching}>
            {logsQuery.isFetching ? '刷新中...' : '刷新'}
          </button>
        </div>
      </div>
      <table className="gmvmax-action-logs__table">
        <thead>
          <tr>
            <th>{GmvMaxTexts.timestampLabel}</th>
            <th>{GmvMaxTexts.actionTypeLabel}</th>
            <th>{GmvMaxTexts.actionTargetLabel}</th>
            <th>{GmvMaxTexts.beforeValueLabel}</th>
            <th>{GmvMaxTexts.afterValueLabel}</th>
            <th>{GmvMaxTexts.operatorLabel}</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.id || `${entry.action_type || entry.type}-${entry.resolved_timestamp}`}>
              <td>{formatTime(entry.resolved_timestamp)}</td>
              <td>{formatActionLabel(entry.action_type || entry.type)}</td>
              <td>{describeTarget(entry)}</td>
              <td>{formatLogValue(entry.before_value ?? entry.before ?? entry.previous_value)}</td>
              <td>{formatLogValue(entry.after_value ?? entry.after ?? entry.new_value)}</td>
              <td>{entry.operator || entry.updated_by || entry.created_by || '-'}</td>
              <td>{buildDescription(entry)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <nav className="gmvmax-action-logs__pagination" aria-label="操作日志分页">
        <button
          type="button"
          onClick={() => setPage((current) => Math.max(1, current - 1))}
          disabled={page <= 1 || logsQuery.isFetching}
        >
          上一页
        </button>
        <span>
          第 {page} / {totalPages} 页
        </span>
        <button
          type="button"
          onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
          disabled={page >= totalPages || logsQuery.isFetching}
        >
          下一页
        </button>
      </nav>
    </div>
  );
}
