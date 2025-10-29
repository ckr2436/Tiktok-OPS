import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';

import {
  listAdvertisers,
  listBusinessCenters,
  listProducts,
  listShops,
} from '../service.js';

const ENTITY_CONFIG = {
  'business-centers': {
    title: 'Business Centers',
    fetcher: listBusinessCenters,
    columns: [
      { key: 'bc_id', label: 'BC ID' },
      { key: 'name', label: '名称' },
      { key: 'status', label: '状态' },
      { key: 'timezone', label: '时区' },
    ],
    baseParams: (authId) => ({ auth_id: authId }),
    filters: [],
  },
  advertisers: {
    title: 'Advertisers',
    fetcher: listAdvertisers,
    columns: [
      { key: 'advertiser_id', label: 'Advertiser ID' },
      { key: 'name', label: '名称' },
      { key: 'status', label: '状态' },
      { key: 'currency', label: '货币' },
    ],
    filters: [{ name: 'bc_id', label: 'BC ID' }],
  },
  shops: {
    title: 'Shops',
    fetcher: listShops,
    columns: [
      { key: 'shop_id', label: 'Shop ID' },
      { key: 'name', label: '名称' },
      { key: 'status', label: '状态' },
      { key: 'region_code', label: '区域' },
    ],
    filters: [{ name: 'advertiser_id', label: 'Advertiser ID' }],
  },
  products: {
    title: 'Products',
    fetcher: listProducts,
    columns: [
      { key: 'product_id', label: 'Product ID' },
      { key: 'title', label: '标题' },
      { key: 'status', label: '状态' },
      { key: 'price', label: '价格' },
    ],
    filters: [{ name: 'shop_id', label: 'Shop ID' }],
  },
};

function cleanParams(obj) {
  const out = {};
  Object.entries(obj || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      out[key] = value;
    }
  });
  return out;
}

export default function AccountDataListPage({ entity }) {
  const { wid, provider, authId } = useParams();
  const normalizedProvider = useMemo(() => provider || 'tiktok-business', [provider]);
  const config = ENTITY_CONFIG[entity];

  const [page, setPage] = useState(1);
  const pageSize = 50;
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const initialFilters = useMemo(() => {
    const defaults = {};
    (config?.filters || []).forEach((f) => {
      defaults[f.name] = '';
    });
    return defaults;
  }, [config]);
  const [filters, setFilters] = useState(initialFilters);
  const [appliedFilters, setAppliedFilters] = useState(initialFilters);

  useEffect(() => {
    setFilters({ ...initialFilters });
    setAppliedFilters({ ...initialFilters });
    setPage(1);
  }, [initialFilters, entity]);

  useEffect(() => {
    if (!config) return;
    let ignore = false;
    async function load() {
      setLoading(true);
      setError('');
      try {
        const baseParams = typeof config.baseParams === 'function' ? config.baseParams(authId) : {};
        const params = {
          page,
          page_size: pageSize,
          ...baseParams,
          ...cleanParams(appliedFilters),
        };
        const data = await config.fetcher(wid, normalizedProvider, params);
        if (!ignore) {
          setItems(Array.isArray(data?.items) ? data.items : []);
          setTotal(Number(data?.total || 0));
        }
      } catch (err) {
        if (!ignore) {
          setError(err?.message || '加载失败');
        }
      } finally {
        if (!ignore) {
          setLoading(false);
        }
      }
    }
    load();
    return () => {
      ignore = true;
    };
  }, [config, wid, normalizedProvider, authId, page, appliedFilters]);

  if (!config) {
    return <div className="p-4">未知数据类型：{entity}</div>;
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  function handleChange(evt) {
    const { name, value } = evt.target;
    setFilters((prev) => ({ ...prev, [name]: value }));
  }

  function handleSubmit(evt) {
    evt.preventDefault();
    setAppliedFilters({ ...filters });
    setPage(1);
  }

  return (
    <div className="p-4 md:p-6 space-y-6">
      <div className="card">
        <div className="text-xl font-semibold">{config.title}</div>
        <div className="small-muted">auth_id：{authId} · Provider：{normalizedProvider}</div>
      </div>

      {config.filters.length > 0 && (
        <div className="card">
          <form style={{ display: 'grid', gap: '16px' }} onSubmit={handleSubmit}>
            {config.filters.map((field) => (
              <div key={field.name} className="grid gap-2">
                <label className="form-label" htmlFor={`filter-${field.name}`}>
                  {field.label}
                </label>
                <input
                  id={`filter-${field.name}`}
                  className="form-input"
                  name={field.name}
                  value={filters[field.name] ?? ''}
                  onChange={handleChange}
                  placeholder="输入过滤条件"
                />
              </div>
            ))}
            <div className="actions">
              <button
                className="btn ghost"
                type="button"
                onClick={() => {
                  setFilters({ ...initialFilters });
                  setAppliedFilters({ ...initialFilters });
                  setPage(1);
                }}
              >
                重置
              </button>
              <button className="btn" type="submit">应用过滤</button>
            </div>
          </form>
        </div>
      )}

      <div className="card space-y-4">
        <div className="flex items-center justify-between">
          <div className="text-base font-semibold">共 {total} 条记录</div>
          <div className="small-muted">第 {page} / {totalPages} 页</div>
        </div>
        {error && <div className="alert alert--error">{error}</div>}
        {loading ? (
          <div className="small-muted">加载中…</div>
        ) : items.length === 0 ? (
          <div className="small-muted">暂无数据</div>
        ) : (
          <div className="table-wrap">
            <table className="data-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  {config.columns.map((col) => (
                    <th
                      key={col.key}
                      style={{
                        textAlign: 'left',
                        borderBottom: '1px solid var(--border)',
                        padding: '8px 12px',
                      }}
                    >
                      {col.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((item, idx) => (
                  <tr
                    key={idx}
                    style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}
                  >
                    {config.columns.map((col) => (
                      <td
                        key={col.key}
                        className="small-muted"
                        style={{ padding: '8px 12px' }}
                      >
                        {item[col.key] !== undefined && item[col.key] !== null ? String(item[col.key]) : '-'}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
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
    </div>
  );
}
