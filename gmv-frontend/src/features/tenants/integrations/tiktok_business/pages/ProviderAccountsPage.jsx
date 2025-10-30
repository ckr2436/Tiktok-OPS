import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import {
  listProviderAccounts,
  getTenantMeta,
  triggerSync,
  normProvider,
} from '../service.js';

const DEFAULT_FORM = {
  scope: 'all',
  mode: 'incremental',
  limit: '',
  product_limit: '',
  shop_id: '',
  since: '',
  idempotency_key: '',
};

const SCOPE_OPTIONS = [
  { value: 'all', label: '全部数据' },
  { value: 'bc', label: 'Business Centers' },
  { value: 'advertisers', label: 'Advertisers' },
  { value: 'shops', label: 'Shops' },
  { value: 'products', label: 'Products' },
];

const MODE_OPTIONS = [
  { value: 'incremental', label: '增量同步' },
  { value: 'full', label: '全量同步' },
];

function SyncDialog({ account, onClose, onSubmit, loading }) {
  const [form, setForm] = useState(DEFAULT_FORM);

  useEffect(() => {
    if (account) {
      setForm(DEFAULT_FORM);
    }
  }, [account]);

  if (!account) return null;

  function handleChange(evt) {
    const { name, value } = evt.target;
    setForm((prev) => ({ ...prev, [name]: value }));
  }

  async function handleSubmit(evt) {
    evt.preventDefault();
    const payload = {
      scope: form.scope || 'all',
      mode: form.mode || 'incremental',
    };
    if (form.limit) {
      const parsed = Number.parseInt(form.limit, 10);
      if (!Number.isNaN(parsed)) payload.limit = parsed;
    }
    if (form.product_limit) {
      const parsed = Number.parseInt(form.product_limit, 10);
      if (!Number.isNaN(parsed)) payload.product_limit = parsed;
    }
    if (form.shop_id?.trim()) {
      payload.shop_id = form.shop_id.trim();
    }
    if (form.since) {
      const date = new Date(form.since);
      if (!Number.isNaN(date.getTime())) {
        payload.since = date.toISOString();
      }
    }
    if (form.idempotency_key?.trim()) {
      payload.idempotency_key = form.idempotency_key.trim();
    }
    await onSubmit(payload);
  }

  return (
    <div className="modal-backdrop">
      <div className="modal">
        <div className="modal__header">
          <div className="modal__title">一键同步</div>
          <button className="btn ghost" onClick={onClose} type="button">关闭</button>
        </div>
        <form className="modal__body space-y-4" onSubmit={handleSubmit}>
          <div>
            <label className="form-label">同步范围</label>
            <select
              className="form-input"
              name="scope"
              value={form.scope}
              onChange={handleChange}
            >
              {SCOPE_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="form-label">模式</label>
            <select
              className="form-input"
              name="mode"
              value={form.mode}
              onChange={handleChange}
            >
              {MODE_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </div>
          <div style={{ display: 'grid', gap: '12px' }}>
            <div>
              <label className="form-label">limit (1-2000)</label>
              <input
                className="form-input"
                name="limit"
                value={form.limit}
                onChange={handleChange}
                placeholder="默认 200"
              />
            </div>
            <div>
              <label className="form-label">product_limit</label>
              <input
                className="form-input"
                name="product_limit"
                value={form.product_limit}
                onChange={handleChange}
                placeholder="仅产品同步"
              />
            </div>
            <div>
              <label className="form-label">shop_id</label>
              <input
                className="form-input"
                name="shop_id"
                value={form.shop_id}
                onChange={handleChange}
                placeholder="可选"
              />
            </div>
            <div>
              <label className="form-label">since</label>
              <input
                className="form-input"
                type="datetime-local"
                name="since"
                value={form.since}
                onChange={handleChange}
              />
            </div>
            <div>
              <label className="form-label">idempotency_key</label>
              <input
                className="form-input"
                name="idempotency_key"
                value={form.idempotency_key}
                onChange={handleChange}
                placeholder="可选"
              />
            </div>
          </div>
          <div className="actions">
            <button className="btn" type="submit" disabled={loading}>立即同步</button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function ProviderAccountsPage() {
  const { wid } = useParams();
  const navigate = useNavigate();

  const normalizedProvider = useMemo(() => normProvider(), []);
  const [workspaceName, setWorkspaceName] = useState('');
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const [accounts, setAccounts] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [dialogAccount, setDialogAccount] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    getTenantMeta(wid)
      .then((meta) => setWorkspaceName(meta?.name || ''))
      .catch(() => setWorkspaceName(''));
  }, [wid]);

  useEffect(() => {
    let ignore = false;
    async function load() {
      setLoading(true);
      setError('');
      try {
        const data = await listProviderAccounts(wid, normalizedProvider, {
          page,
          page_size: pageSize,
        });
        if (!ignore) {
          setAccounts(Array.isArray(data?.items) ? data.items : []);
          setTotal(Number(data?.total || 0));
        }
      } catch (err) {
        if (!ignore) setError(err?.message || '加载失败');
      } finally {
        if (!ignore) setLoading(false);
      }
    }
    load();
    return () => {
      ignore = true;
    };
  }, [wid, normalizedProvider, page, refreshKey]);

  async function handleSubmitSync(payload) {
    if (!dialogAccount) return;
    setSubmitting(true);
    try {
      const { scope = 'all', ...rest } = payload || {};
      const result = await triggerSync(wid, normalizedProvider, dialogAccount.auth_id, scope, rest);
      const runId = result?.run_id;
      if (runId) {
        navigate(`/tenants/${encodeURIComponent(wid)}/integrations/${encodeURIComponent(normalizedProvider)}/accounts/${dialogAccount.auth_id}/runs/${runId}`);
        setDialogAccount(null);
      }
    } catch (err) {
      // eslint-disable-next-line no-alert
      alert(err?.message || '同步失败');
    } finally {
      setSubmitting(false);
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="p-4 md:p-6 space-y-6">
      <div className="card">
        <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
          <div>
            <div className="text-xl font-semibold">Provider Accounts</div>
            <div className="small-muted">
              工作区：{workspaceName || wid} · Provider：{normalizedProvider}
            </div>
          </div>
          <div className="flex items-center gap-3">
            <button
              className="btn ghost"
              type="button"
              onClick={() => setRefreshKey((val) => val + 1)}
              disabled={loading}
            >
              刷新
            </button>
          </div>
        </div>
      </div>

      <div className="card space-y-4">
        <div className="flex items-center justify-between">
          <div className="text-base font-semibold">绑定账户（共 {total} 个）</div>
          <div className="small-muted">第 {page} / {totalPages} 页</div>
        </div>
        {error && <div className="alert alert--error">{error}</div>}
        {loading ? (
          <div className="small-muted">加载中…</div>
        ) : accounts.length === 0 ? (
          <div className="small-muted">暂无绑定账户</div>
        ) : (
          <div style={{ display: 'grid', gap: '16px' }}>
            {accounts.map((acc) => (
              <div
                key={acc.auth_id}
                style={{
                  border: '1px solid var(--border)',
                  borderRadius: '12px',
                  padding: '16px',
                  display: 'grid',
                  gap: '12px',
                }}
              >
                <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
                  <div>
                    <div className="text-lg font-medium">{acc.label || `Account ${acc.auth_id}`}</div>
                    <div className="small-muted">auth_id: {acc.auth_id} · 状态：{acc.status}</div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button className="btn" type="button" onClick={() => setDialogAccount(acc)}>
                      Sync Now
                    </button>
                    <Link
                      className="btn ghost"
                      to={`/tenants/${encodeURIComponent(wid)}/integrations/${encodeURIComponent(normalizedProvider)}/accounts/${acc.auth_id}/business-centers`}
                    >
                      Business Centers
                    </Link>
                    <Link
                      className="btn ghost"
                      to={`/tenants/${encodeURIComponent(wid)}/integrations/${encodeURIComponent(normalizedProvider)}/accounts/${acc.auth_id}/advertisers`}
                    >
                      Advertisers
                    </Link>
                    <Link
                      className="btn ghost"
                      to={`/tenants/${encodeURIComponent(wid)}/integrations/${encodeURIComponent(normalizedProvider)}/accounts/${acc.auth_id}/shops`}
                    >
                      Shops
                    </Link>
                    <Link
                      className="btn ghost"
                      to={`/tenants/${encodeURIComponent(wid)}/integrations/${encodeURIComponent(normalizedProvider)}/accounts/${acc.auth_id}/products`}
                    >
                      Products
                    </Link>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="flex items-center justify-between">
          <button
            className="btn ghost"
            type="button"
            onClick={() => setPage((prev) => Math.max(1, prev - 1))}
            disabled={loading || page <= 1}
          >
            上一页
          </button>
          <button
            className="btn ghost"
            type="button"
            onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
            disabled={loading || page >= totalPages}
          >
            下一页
          </button>
        </div>
      </div>

      {dialogAccount && (
        <SyncDialog
          account={dialogAccount}
          onClose={() => setDialogAccount(null)}
          onSubmit={handleSubmitSync}
          loading={submitting}
        />
      )}
    </div>
  );
}
