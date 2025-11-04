import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';

import ProductDetailDrawer from '../../../gmvMax/ProductDetailDrawer.jsx';
import ProductRow from '../../../gmvMax/components/ProductRow.jsx';
import OccupiedDialog from '../../../gmvMax/components/OccupiedDialog.jsx';
import useGmvMaxProducts from '../../../gmvMax/useGmvMaxProducts.js';
import {
  fetchAdvertisers,
  fetchBusinessCenters,
  fetchStores,
  listBindings,
  normProvider,
} from '../service.js';

function getErrorMessage(error, fallback = '操作失败') {
  if (!error) return fallback;
  if (typeof error === 'string') return error;
  if (typeof error.message === 'string' && error.message) return error.message;
  if (typeof error.detail === 'string' && error.detail) return error.detail;
  if (typeof error.text === 'string' && error.text) return error.text;
  return fallback;
}

function formatBcLabel(item) {
  if (!item) return '';
  const name = item.name || item.raw?.bc_info?.name || item.raw?.name;
  const id = item.bc_id || item.id;
  if (name && id) return `${name}（${id}）`;
  if (name) return name;
  return id || '';
}

function formatAdvertiserLabel(item) {
  if (!item) return '';
  const name = item.display_name || item.name || item.raw?.advertiser_name;
  const id = item.advertiser_id || item.id;
  if (name && id) return `${name}（${id}）`;
  if (name) return name;
  return id || '';
}

function formatStoreLabel(item) {
  if (!item) return '';
  const name = item.name || item.store_name;
  const code = item.store_code;
  const id = item.store_id || item.id;
  if (name && code) return `${name}（${code}）`;
  if (name && id) return `${name}（${id}）`;
  return name || code || id || '';
}

const STATUS_OPTIONS = [
  { value: 'ALL', label: '全部商品' },
  { value: 'AVAILABLE', label: '可投放' },
  { value: 'NOT_AVAILABLE', label: '不可投放' },
];

const OCCUPIED_OPTIONS = [
  { value: 'ALL', label: '全部占用状态' },
  { value: 'OCCUPIED', label: '仅已占用' },
  { value: 'UNOCCUPIED', label: '仅未占用' },
];

const SORT_FIELDS = [
  { value: 'min_price', label: '按价格' },
  { value: 'historical_sales', label: '按历史销量' },
];

export default function GmvMaxManagementPage() {
  const { wid, itemGroupId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const provider = useMemo(() => normProvider(), []);

  const [authBindings, setAuthBindings] = useState([]);
  const [selectedAuthId, setSelectedAuthId] = useState('');
  const [authLoading, setAuthLoading] = useState(false);

  const [businessCenters, setBusinessCenters] = useState([]);
  const [advertisers, setAdvertisers] = useState([]);
  const [stores, setStores] = useState([]);
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [storesLoading, setStoresLoading] = useState(false);

  const [selectedBcId, setSelectedBcId] = useState('');
  const [selectedAdvertiserId, setSelectedAdvertiserId] = useState('');
  const [selectedStoreId, setSelectedStoreId] = useState('');

  const [banner, setBanner] = useState(null);
  const [toast, setToast] = useState(null);
  const toastTimer = useRef(null);

  const [occupiedProduct, setOccupiedProduct] = useState(null);
  const [occupiedOpen, setOccupiedOpen] = useState(false);

  const [syncing, setSyncing] = useState(false);
  const [selectorsOpen, setSelectorsOpen] = useState(false);

  const {
    items,
    pageInfo,
    loading: loadingProducts,
    error: productsError,
    filters,
    sort,
    setFilters,
    setPage,
    setSort,
    refresh,
  } = useGmvMaxProducts({
    bcId: selectedBcId,
    advertiserId: selectedAdvertiserId,
    storeId: selectedStoreId,
    page: 1,
    pageSize: 20,
  });

  useEffect(() => () => {
    if (toastTimer.current) {
      clearTimeout(toastTimer.current);
      toastTimer.current = null;
    }
  }, []);

  const showToast = useCallback((type, message) => {
    if (toastTimer.current) {
      clearTimeout(toastTimer.current);
    }
    setToast({ type, message });
    toastTimer.current = setTimeout(() => {
      setToast(null);
      toastTimer.current = null;
    }, 2400);
  }, []);

  useEffect(() => {
    if (!wid) return;
    setAuthLoading(true);
    listBindings(wid)
      .then((list) => {
        setAuthBindings(Array.isArray(list) ? list : []);
        setSelectedAuthId((prev) => {
          if (prev && list.some((item) => String(item.auth_id) === String(prev))) {
            return prev;
          }
          const first = list?.[0];
          return first ? String(first.auth_id) : '';
        });
      })
      .catch((error) => {
        setAuthBindings([]);
        setSelectedAuthId('');
        setBanner({ type: 'error', message: getErrorMessage(error, '无法获取授权账号列表') });
      })
      .finally(() => setAuthLoading(false));
  }, [wid]);

  useEffect(() => {
    if (!wid || !selectedAuthId) {
      setBusinessCenters([]);
      setAdvertisers([]);
      setStores([]);
      setSelectedBcId('');
      setSelectedAdvertiserId('');
      setSelectedStoreId('');
      return;
    }

    const controller = new AbortController();
    setOptionsLoading(true);
    Promise.all([
      fetchBusinessCenters(wid, provider, selectedAuthId, { signal: controller.signal }),
      fetchAdvertisers(wid, provider, selectedAuthId, {}, { signal: controller.signal }),
    ])
      .then(([bcResponse, advResponse]) => {
        const bcList = Array.isArray(bcResponse?.items) ? bcResponse.items : [];
        const advList = Array.isArray(advResponse?.items) ? advResponse.items : [];
        setBusinessCenters(bcList);
        setAdvertisers(advList);
        setSelectedBcId((prev) => {
          if (prev && bcList.some((item) => String(item.bc_id) === String(prev))) {
            return prev;
          }
          return '';
        });
        setSelectedAdvertiserId((prev) => {
          if (prev && advList.some((item) => String(item.advertiser_id) === String(prev))) {
            return prev;
          }
          return '';
        });
        setStores([]);
        setSelectedStoreId('');
      })
      .catch((error) => {
        if (error?.name === 'AbortError') return;
        setBusinessCenters([]);
        setAdvertisers([]);
        setStores([]);
        setSelectedBcId('');
        setSelectedAdvertiserId('');
        setSelectedStoreId('');
        setBanner({ type: 'error', message: getErrorMessage(error, '无法获取 BC / Advertiser 列表') });
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setOptionsLoading(false);
        }
      });

    return () => controller.abort();
  }, [provider, selectedAuthId, wid]);

  const filteredAdvertisers = useMemo(() => {
    if (!selectedBcId) return advertisers;
    return (advertisers || []).filter(
      (item) => String(item.bc_id || item.owner_bc_id || '') === String(selectedBcId),
    );
  }, [advertisers, selectedBcId]);

  const selectedBc = useMemo(
    () => businessCenters.find((item) => String(item.bc_id || item.id) === String(selectedBcId)) || null,
    [businessCenters, selectedBcId],
  );

  const selectedAdvertiser = useMemo(
    () => filteredAdvertisers.find((item) => String(item.advertiser_id || item.id) === String(selectedAdvertiserId)) || null,
    [filteredAdvertisers, selectedAdvertiserId],
  );

  const selectedStore = useMemo(
    () => stores.find((item) => String(item.store_id || item.id) === String(selectedStoreId)) || null,
    [stores, selectedStoreId],
  );

  const selectorSummary = useMemo(() => {
    const bcText = formatBcLabel(selectedBc) || '未选择';
    const advText = formatAdvertiserLabel(selectedAdvertiser) || '未选择';
    const storeText = formatStoreLabel(selectedStore) || '未选择';
    return `BC: ${bcText} · Adv: ${advText} · Store: ${storeText}`;
  }, [selectedBc, selectedAdvertiser, selectedStore]);

  useEffect(() => {
    setSelectedAdvertiserId((prev) => {
      if (prev && filteredAdvertisers.some((item) => String(item.advertiser_id) === String(prev))) {
        return prev;
      }
      return '';
    });
  }, [filteredAdvertisers]);

  useEffect(() => {
    if (!wid || !selectedAuthId || !selectedAdvertiserId) {
      setStores([]);
      setSelectedStoreId('');
      return;
    }

    const controller = new AbortController();
    setStoresLoading(true);
    fetchStores(wid, provider, selectedAuthId, selectedAdvertiserId, {}, { signal: controller.signal })
      .then((response) => {
        const list = Array.isArray(response?.items) ? response.items : [];
        setStores(list);
        setSelectedStoreId((prev) => {
          if (prev && list.some((item) => String(item.store_id) === String(prev))) {
            return prev;
          }
          return '';
        });
      })
      .catch((error) => {
        if (error?.name === 'AbortError') return;
        setStores([]);
        setSelectedStoreId('');
        setBanner({ type: 'error', message: getErrorMessage(error, '加载 Store 列表失败') });
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setStoresLoading(false);
        }
      });

    return () => controller.abort();
  }, [provider, selectedAdvertiserId, selectedAuthId, wid]);

  useEffect(() => {
    if (!loadingProducts) {
      setSyncing(false);
    }
  }, [loadingProducts]);

  useEffect(() => {
    if (selectorsOpen && selectedStoreId) {
      setSelectorsOpen(false);
    }
  }, [selectorsOpen, selectedStoreId]);

  useEffect(() => {
    if (!selectedStoreId) {
      setFilters({ status: 'ALL', occupied: 'ALL', query: '' });
    }
  }, [selectedStoreId, setFilters]);

  const selectedProduct = useMemo(() => {
    if (!itemGroupId) return null;
    return items.find((item) => String(item.itemGroupId) === String(itemGroupId)) || null;
  }, [items, itemGroupId]);

  const handleSyncOnce = useCallback(() => {
    if (!selectedBcId || !selectedAdvertiserId || !selectedStoreId) {
      showToast('warn', '请选择完整的 BC / Advertiser / Store 后再同步');
      return;
    }
    setSyncing(true);
    refresh();
  }, [refresh, selectedAdvertiserId, selectedBcId, selectedStoreId, showToast]);

  const handleViewOccupied = useCallback((product) => {
    setOccupiedProduct(product);
    setOccupiedOpen(true);
  }, []);

  const handleCreatePlan = useCallback(() => {
    showToast('info', 'TODO：新建 GMV Max 计划');
  }, [showToast]);

  const handleAutomation = useCallback(() => {
    showToast('info', 'TODO：自动化设置');
  }, [showToast]);

  const handleOpenDetail = useCallback(
    (product) => {
      if (!wid || !product?.itemGroupId) return;
      navigate(`/tenants/${wid}/gmv-max/products/${product.itemGroupId}`, { state: { background: location } });
    },
    [navigate, wid, location],
  );

  const handleCloseDetail = useCallback(() => {
    if (!wid) return;
    navigate(`/tenants/${wid}/gmv-max`);
  }, [navigate, wid]);

  const disablePrev = pageInfo.page <= 1;
  const disableNext = pageInfo.page >= pageInfo.totalPages || pageInfo.totalPages === 0;

  return (
    <div className="gmv-page">
      <header className="gmv-toolbar">
        <div className="gmv-toolbar__primary">
          <div className={`gmv-selector${selectorsOpen ? ' is-open' : ''}`}>
            <button
              type="button"
              className="gmv-selector__summary"
              onClick={() => setSelectorsOpen((prev) => !prev)}
              aria-expanded={selectorsOpen}
            >
              <span className="gmv-selector__label">当前范围</span>
              <span className="gmv-selector__text">{selectorSummary}</span>
              <span className="gmv-selector__caret" aria-hidden="true" />
            </button>
            {selectorsOpen ? (
              <div className="gmv-selector__panel">
                <label className="gmv-toolbar__field">
                  <span className="gmv-toolbar__label">授权账号</span>
                  <select
                    value={selectedAuthId}
                    onChange={(event) => setSelectedAuthId(event.target.value)}
                    disabled={authLoading || !authBindings.length}
                  >
                    <option value="" disabled>
                      {authLoading ? '加载中…' : '请选择授权账号'}
                    </option>
                    {authBindings.map((binding) => (
                      <option key={binding.auth_id} value={binding.auth_id}>
                        {binding.alias || binding.provider_app_name || binding.auth_id}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="gmv-toolbar__field">
                  <span className="gmv-toolbar__label">Business Center</span>
                  <select
                    value={selectedBcId}
                    onChange={(event) => {
                      setSelectedBcId(event.target.value);
                      setSelectedAdvertiserId('');
                      setSelectedStoreId('');
                      setStores([]);
                    }}
                    disabled={optionsLoading || !businessCenters.length}
                  >
                    <option value="">全部 BC</option>
                    {businessCenters.map((bc) => (
                      <option key={bc.bc_id || bc.id} value={bc.bc_id || bc.id}>
                        {formatBcLabel(bc) || (bc.bc_id || bc.id)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="gmv-toolbar__field">
                  <span className="gmv-toolbar__label">Advertiser</span>
                  <select
                    value={selectedAdvertiserId}
                    onChange={(event) => {
                      setSelectedAdvertiserId(event.target.value);
                      setSelectedStoreId('');
                    }}
                    disabled={optionsLoading || !filteredAdvertisers.length}
                  >
                    <option value="" disabled>
                      {optionsLoading ? '加载中…' : '请选择 Advertiser'}
                    </option>
                    {filteredAdvertisers.map((adv) => (
                      <option key={adv.advertiser_id || adv.id} value={adv.advertiser_id || adv.id}>
                        {formatAdvertiserLabel(adv) || (adv.advertiser_id || adv.id)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="gmv-toolbar__field">
                  <span className="gmv-toolbar__label">Store</span>
                  <select
                    value={selectedStoreId}
                    onChange={(event) => setSelectedStoreId(event.target.value)}
                    disabled={storesLoading || !stores.length}
                  >
                    <option value="" disabled>
                      {storesLoading ? '加载中…' : '请选择 Store'}
                    </option>
                    {stores.map((store) => (
                      <option key={store.store_id || store.id} value={store.store_id || store.id}>
                        {formatStoreLabel(store) || (store.store_id || store.id)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            ) : null}
          </div>
          <div className="gmv-filter-bar">
            <label className="gmv-filter-bar__field">
              <span className="gmv-filter-bar__label">可投放</span>
              <select
                value={filters.status}
                onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
                disabled={!selectedStoreId}
              >
                {STATUS_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="gmv-filter-bar__field">
              <span className="gmv-filter-bar__label">GMV Max 占用</span>
              <select
                value={filters.occupied}
                onChange={(event) => setFilters((prev) => ({ ...prev, occupied: event.target.value }))}
                disabled={!selectedStoreId}
              >
                {OCCUPIED_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
        <div className="gmv-toolbar__secondary">
          <div className="gmv-filter-bar__search">
            <input
              type="search"
              className="form-input"
              placeholder="搜索商品名称"
              value={filters.query}
              onChange={(event) => setFilters((prev) => ({ ...prev, query: event.target.value }))}
              disabled={!selectedStoreId}
            />
          </div>
          <div className="gmv-sorter">
            <select
              value={sort.field}
              onChange={(event) => setSort(event.target.value, sort.type)}
              disabled={!selectedStoreId}
            >
              {SORT_FIELDS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn ghost"
              onClick={() => setSort(sort.field, sort.type === 'ASC' ? 'DESC' : 'ASC')}
              disabled={!selectedStoreId}
            >
              {sort.type === 'ASC' ? '升序' : '降序'}
            </button>
          </div>
          <button
            type="button"
            className="btn"
            onClick={handleSyncOnce}
            disabled={syncing || loadingProducts}
          >
            {syncing || loadingProducts ? '同步中…' : '同步一次'}
          </button>
        </div>
      </header>

      {banner ? (
        <div className={`alert ${banner.type === 'error' ? 'alert--error' : ''}`} role="alert">
          {banner.message}
        </div>
      ) : null}

        <div className="gmv-list">
          <div className="gmv-list__header">
            <div>
              {selectedStoreId
              ? `共 ${pageInfo.total} 条记录，当前展示 ${items.length} 条`
              : '请选择 Store 查看商品列表'}
            </div>
          </div>

          {!selectedStoreId ? (
          <div className="overview-state">
            <div className="overview-state__message">请选择 Business Center / Advertiser / Store 以加载 GMV Max 商品。</div>
          </div>
        ) : loadingProducts ? (
          <div className="skeleton-list" aria-label="加载中">
            {Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="gmv-skeleton-row">
                <div className="skeleton-line" style={{ width: '60%' }} />
                <div className="skeleton-line" style={{ width: '40%' }} />
              </div>
            ))}
          </div>
        ) : productsError ? (
          <div className="overview-state overview-state--error">
            <div className="overview-state__message">{getErrorMessage(productsError, '商品列表加载失败')}</div>
          </div>
        ) : items.length === 0 ? (
          <div className="overview-state">
            <div className="overview-state__message">未找到符合条件的商品。</div>
          </div>
        ) : (
          <div className="gmv-product-list">
            {items.map((product) => (
              <ProductRow
                key={product.itemGroupId}
                product={product}
                onOpenDetail={handleOpenDetail}
                onViewOccupied={handleViewOccupied}
                onCreatePlan={handleCreatePlan}
                onOpenAutomation={handleAutomation}
              />
            ))}
          </div>
        )}
      </div>

      {selectedStoreId && pageInfo.totalPages > 1 ? (
        <footer className="gmv-pagination">
          <span>
            第 {pageInfo.page} / {Math.max(pageInfo.totalPages, 1)} 页
          </span>
          <div className="gmv-pagination__controls">
            <button type="button" className="btn ghost" onClick={() => setPage(pageInfo.page - 1)} disabled={disablePrev}>
              上一页
            </button>
            <button type="button" className="btn ghost" onClick={() => setPage(pageInfo.page + 1)} disabled={disableNext}>
              下一页
            </button>
          </div>
        </footer>
      ) : null}

      {toast ? (
        <div className={`toast toast--${toast.type}`} role="status">
          {toast.message}
        </div>
      ) : null}

      <OccupiedDialog open={occupiedOpen} product={occupiedProduct} onClose={() => setOccupiedOpen(false)} />

      <ProductDetailDrawer open={Boolean(itemGroupId)} product={selectedProduct} onClose={handleCloseDetail} />
    </div>
  );
}
