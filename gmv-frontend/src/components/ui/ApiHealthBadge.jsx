// src/components/ui/ApiHealthBadge.jsx
import { useEffect, useMemo, useState } from 'react';

const POLL_INTERVAL = 10000;

const STATUS_LABELS = {
  online: 'Online',
  offline: 'Offline',
  loading: 'Checking…',
};

export default function ApiHealthBadge() {
  const [status, setStatus] = useState('loading');

  useEffect(() => {
    let active = true;
    let timer = null;

    async function fetchHealth() {
      try {
        const res = await fetch('/api/healthz', { cache: 'no-store' });
        if (!res.ok) throw new Error('Health check failed');

        const payload = await res.json();
        if (!active) return;
        setStatus(payload?.ok ? 'online' : 'offline');
      } catch (err) {
        if (!active) return;
        setStatus('offline');
      } finally {
        if (!active) return;
        timer = window.setTimeout(fetchHealth, POLL_INTERVAL);
      }
    }

    fetchHealth();

    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  const label = useMemo(() => STATUS_LABELS[status] || STATUS_LABELS.loading, [status]);

  return (
    <div className={`api-health api-health--${status}`} role="status" aria-live="polite">
      <span className="api-health__dot" aria-hidden />
      <span className="api-health__text">{label}</span>
    </div>
  );
}
