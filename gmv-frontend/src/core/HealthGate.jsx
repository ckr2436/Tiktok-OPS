import PropTypes from 'prop-types';
import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';

const HEALTH_POLL_INTERVAL = 5000;

async function fetchHealth() {
  const res = await fetch('/api/healthz', { cache: 'no-store' });
  if (!res.ok) throw new Error('Health check failed');
  const data = await res.json();
  if (!data?.ok) throw new Error('Health check returned not ok');
  return data;
}

export default function HealthGate({ children }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['api-health', 'gate'],
    queryFn: fetchHealth,
    retry: false,
    refetchInterval: (query) => (query.state.status === 'error' ? HEALTH_POLL_INTERVAL : false),
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: false,
  });

  const state = useMemo(() => {
    if (isLoading) return 'checking';
    if (isError) return 'unavailable';
    return data?.ok ? 'online' : 'unavailable';
  }, [data, isError, isLoading]);

  const isBlocked = state !== 'online';

  return (
    <div className="health-gate-wrap" aria-busy={isBlocked}>
      {children}
      {isBlocked && (
        <div className="health-gate" role="alert" aria-live="assertive">
          <div className="health-gate__panel">
            <div className="health-gate__spinner" aria-hidden />
            <div className="health-gate__content">
              <p className="health-gate__title">正在检查服务状态</p>
              <p className="health-gate__desc">
                {state === 'checking'
                  ? '请稍候，我们正在确认服务可用性。'
                  : '服务暂时不可用，正在自动重试…'}
              </p>
              <p className="health-gate__hint">如问题持续，请稍后再试。</p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

HealthGate.propTypes = {
  children: PropTypes.node.isRequired,
};
