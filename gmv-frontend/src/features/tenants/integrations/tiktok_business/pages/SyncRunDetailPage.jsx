import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { getSyncRun, normProvider } from '../service.js';

const TERMINAL = new Set(['success', 'failed']);
const POLL_INTERVAL = 5000;

function fmt(dt) {
  if (!dt) return '-';
  try {
    return new Date(dt).toLocaleString();
  } catch (err) {
    return String(dt);
  }
}

function SummaryTable({ stats }) {
  if (!stats) return null;
  const processed = stats.processed || {};
  const summary = processed.summary || {};
  const counts = processed.counts || {};
  const timings = processed.timings || {};
  const totalMs = timings.total_ms ?? '-';
  const partial = summary.partial ? 'Yes' : 'No';
  return (
    <div style={{ display: 'grid', gap: '12px' }}>
      <div className="text-base font-semibold">Processed Summary</div>
      <div className="small-muted">Partial: {partial}</div>
      <div className="small-muted">Total Duration: {totalMs} ms</div>
      {Object.entries(counts).map(([scope, row]) => (
        <div
          key={scope}
          style={{
            border: '1px dashed var(--border)',
            borderRadius: '10px',
            padding: '12px',
          }}
        >
          <div className="font-medium mb-2">{scope}</div>
          <ul className="small-muted" style={{ display: 'grid', gap: '6px', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))' }}>
            <li>Fetched: {row.fetched ?? 0}</li>
            <li>Upserts: {row.upserts ?? 0}</li>
            <li>Skipped: {row.skipped ?? 0}</li>
          </ul>
        </div>
      ))}
    </div>
  );
}

function ErrorList({ errors }) {
  if (!errors || errors.length === 0) return null;
  return (
    <div style={{ display: 'grid', gap: '10px' }}>
      <div className="text-base font-semibold">Errors</div>
      {errors.map((err, idx) => (
        <div key={idx} className="alert alert--error">
          <div>Stage: {err.stage}</div>
          <div>Code: {err.code}</div>
          <div>Message: {err.message}</div>
        </div>
      ))}
    </div>
  );
}

export default function SyncRunDetailPage() {
  const { wid, authId, runId } = useParams();
  const normalizedProvider = useMemo(() => normProvider(), []);

  const [run, setRun] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [lastFetchedAt, setLastFetchedAt] = useState(null);

  useEffect(() => {
    let active = true;
    let timer = null;

    async function fetchRun() {
      setLoading(true);
      try {
        const data = await getSyncRun(wid, normalizedProvider, authId, runId);
        if (!active) return;
        setRun(data);
        setError('');
        setLastFetchedAt(new Date());
        if (TERMINAL.has(String(data?.status || '').toLowerCase())) {
          if (timer) clearTimeout(timer);
          timer = null;
          setLoading(false);
          return;
        }
        timer = setTimeout(fetchRun, POLL_INTERVAL);
        setLoading(false);
      } catch (err) {
        if (!active) return;
        setError(err?.message || '加载失败');
        setLoading(false);
      }
    }

    fetchRun();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [wid, normalizedProvider, authId, runId]);

  const status = String(run?.status || '').toLowerCase();
  const stats = run?.stats || {};
  const errors = stats.errors || run?.errors || [];

  return (
    <div className="p-4 md:p-6 space-y-6">
      <div className="card space-y-2">
        <div className="text-xl font-semibold">Sync Run #{runId}</div>
        <div className="small-muted">
          Provider：{normalizedProvider} · auth_id：{authId}
        </div>
        <div className="small-muted">
          状态：{status || '-'} · 更新时间：{fmt(lastFetchedAt)}
        </div>
        <div className="small-muted">
          Scheduled：{fmt(run?.scheduled_for)} · Enqueued：{fmt(run?.enqueued_at)}
        </div>
        <div className="small-muted">Duration：{run?.duration_ms ?? '-'} ms</div>
        {run?.error_message && (
          <div className="alert alert--error">{run.error_message}</div>
        )}
        <div className="flex flex-wrap gap-2 pt-2">
          <Link
            className="btn ghost"
            to={`/tenants/${encodeURIComponent(wid)}/integrations/${encodeURIComponent(normalizedProvider)}/accounts/${authId}/overview`}
          >
            查看 Overview
          </Link>
        </div>
      </div>

      {loading && <div className="small-muted">加载中…</div>}
      {error && <div className="alert alert--error">{error}</div>}

      {!loading && !error && (
        <div className="grid gap-6">
          <SummaryTable stats={stats} />
          <ErrorList errors={errors} />
        </div>
      )}
    </div>
  );
}
