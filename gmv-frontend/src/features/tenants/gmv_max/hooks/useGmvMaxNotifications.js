import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useGmvMaxActionLogsQuery } from './gmvMaxQueries.js';

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
      return type || '执行操作';
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

function formatLogValue(value) {
  if (value === undefined || value === null) return '';
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

function isActionableEntry(entry) {
  const action = String(entry?.action_type || entry?.type || '').toUpperCase();
  const result = String(entry?.result || '').toUpperCase();
  return action !== 'HOLD' && result !== 'SKIPPED';
}

function buildNotification(entry) {
  const type = entry?.action_type || entry?.type;
  const before = entry?.before_value ?? entry?.before ?? entry?.previous_value;
  const after = entry?.after_value ?? entry?.after ?? entry?.new_value;
  const reason = entry?.reason || entry?.comment || entry?.note;
  const operator = entry?.operator || entry?.updated_by || entry?.created_by;
  const parts = [describeTarget(entry), formatActionLabel(type)];
  if (before !== undefined || after !== undefined) {
    parts.push(`从 ${formatLogValue(before) || '-'} 调整为 ${formatLogValue(after) || '-'}`);
  }
  if (reason) parts.push(`原因：${reason}`);
  if (operator) parts.push(`执行方：${operator}`);
  return parts.filter(Boolean).join('；');
}

export function useGmvMaxNotifications({
  workspaceId,
  provider,
  authId,
  campaignId,
  enabled = true,
  pollInterval = 60_000,
} = {}) {
  const [notification, setNotification] = useState(null);
  const lastSeenRef = useRef(null);

  const logsQuery = useGmvMaxActionLogsQuery(
    workspaceId,
    provider,
    authId,
    campaignId,
    { page_size: 10, sort: '-timestamp' },
    {
      enabled: Boolean(enabled && workspaceId && provider && authId && campaignId),
      refetchInterval: enabled ? pollInterval : false,
      refetchIntervalInBackground: true,
    },
  );

  useEffect(() => {
    lastSeenRef.current = null;
    setNotification(null);
  }, [workspaceId, provider, authId, campaignId]);

  const latestEntry = useMemo(() => {
    const entries = ensureArray(logsQuery.data?.entries || logsQuery.data?.items);
    if (!entries.length) return null;
    return entries
      .filter(isActionableEntry)
      .map((entry) => ({ entry, ts: resolveTimestamp(entry) }))
      .filter((item) => item.ts)
      .sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())[0] || null;
  }, [logsQuery.data]);

  useEffect(() => {
    if (!latestEntry?.ts) return;
    if (!lastSeenRef.current) {
      lastSeenRef.current = latestEntry.ts;
      return;
    }
    const latestTime = new Date(latestEntry.ts).getTime();
    const previousTime = new Date(lastSeenRef.current).getTime();
    if (Number.isFinite(latestTime) && Number.isFinite(previousTime) && latestTime > previousTime) {
      lastSeenRef.current = latestEntry.ts;
      setNotification({
        message: buildNotification(latestEntry.entry),
        timestamp: latestEntry.ts,
        entry: latestEntry.entry,
      });
    }
  }, [latestEntry]);

  const dismiss = useCallback(() => setNotification(null), []);

  return {
    notification,
    dismiss,
    isLoading: logsQuery.isFetching,
    error: logsQuery.error,
  };
}

export default useGmvMaxNotifications;
