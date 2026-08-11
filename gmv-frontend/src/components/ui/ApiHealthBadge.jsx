// src/components/ui/ApiHealthBadge.jsx
import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';

const POLL_INTERVAL = 120000;

const STATUS_LABELS = {
  online: 'Online',
  offline: 'Offline',
  loading: 'Checking…',
};

async function fetchApiHealth() {
  const res = await fetch('/api/healthz', { cache: 'no-store' });
  if (!res.ok) throw new Error('Health check failed');
  return res.json();
}

export default function ApiHealthBadge() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['api-health'],
    queryFn: fetchApiHealth,
    refetchInterval: POLL_INTERVAL,
    refetchIntervalInBackground: true,
  });

  const status = useMemo(() => {
    if (isLoading) return 'loading';
    if (isError) return 'offline';
    return data?.ok ? 'online' : 'offline';
  }, [data, isError, isLoading]);

  const label = useMemo(() => STATUS_LABELS[status] || STATUS_LABELS.loading, [status]);

  return (
    <div className={`api-health api-health--${status}`} role="status" aria-live="polite">
      <span className="api-health__dot" aria-hidden />
      <span className="api-health__text">{label}</span>
    </div>
  );
}
