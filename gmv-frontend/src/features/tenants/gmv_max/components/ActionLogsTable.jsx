import { useMemo } from 'react';

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
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatLogValue(value) {
  if (value === undefined || value === null) return '—';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch (error) {
      return String(value);
    }
  }
  if (typeof value === 'number') {
    return Number(value).toLocaleString();
  }
  return String(value);
}

function formatActionLabel(type) {
  switch ((type || '').toUpperCase()) {
    case 'HEAT':
    case 'BOOST':
    case 'BOOST_CREATIVE':
      return '加热';
    case 'STOP_HEAT':
    case 'STOP':
      return '停止加热';
    case 'PAUSE':
      return '暂停系列';
    case 'RESUME':
      return '启用系列';
    case 'INCREASE_BUDGET':
      return '提升预算';
    case 'DECREASE_BUDGET':
      return '降低预算';
    case 'RAISE_BUDGET':
      return '提升预算';
    default:
      return type || '—';
  }
}

function describeTarget(entry) {
  if (entry?.creative_id || entry?.creativeId) {
    return `素材 ${entry.creative_id || entry.creativeId}`;
  }
  if (entry?.campaign_id || entry?.campaignId) {
    return `系列 ${entry.campaign_id || entry.campaignId}`;
  }
  return '系列';
}

function buildDescription(entry) {
  const type = entry?.action_type || entry?.type;
  const before = entry?.before_value ?? entry?.before ?? entry?.previous_value;
  const after = entry?.after_value ?? entry?.after ?? entry?.new_value;
  const reason = entry?.reason || entry?.comment || entry?.note;
  const operator = entry?.operator || entry?.updated_by || entry?.created_by;
  const pieces = [formatActionLabel(type)];
  if (before !== undefined || after !== undefined) {
    pieces.push(`从 ${formatLogValue(before)} 调整到 ${formatLogValue(after)}`);
  }
  if (reason) {
    pieces.push(`原因：${reason}`);
  }
  if (operator) {
    pieces.push(`操作者：${operator}`);
  }
  return pieces.join('，');
}

export default function ActionLogsTable({
  workspaceId,
  provider,
  authId,
  campaignId,
  params = {},
  pageSize = 20,
}) {
  const queryParams = useMemo(
    () => ({
      page_size: pageSize,
      sort: '-timestamp',
      ...params,
    }),
    [pageSize, params],
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

  if (logsQuery.isLoading) {
    return <Loading text="操作日志加载中…" />;
  }

  if (logsQuery.error) {
    return (
      <div className="gmvmax-error">
        <span>{logsQuery.error.message || '操作日志加载失败'}</span>
        <button type="button" onClick={() => logsQuery.refetch()} className="gmvmax-error__retry">
          {GmvMaxTexts.retry}
        </button>
      </div>
    );
  }

  if (!entries.length) {
    return <p>暂无操作记录。</p>;
  }

  return (
    <div className="gmvmax-action-logs">
      <div className="gmvmax-action-logs__header">
        <h3>{GmvMaxTexts.campaignActionLogs}</h3>
        <button type="button" onClick={() => logsQuery.refetch()} disabled={logsQuery.isFetching}>
          {logsQuery.isFetching ? '刷新中…' : '刷新'}
        </button>
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
            <th>详情</th>
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
              <td>{entry.operator || entry.updated_by || entry.created_by || '—'}</td>
              <td>{buildDescription(entry)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
