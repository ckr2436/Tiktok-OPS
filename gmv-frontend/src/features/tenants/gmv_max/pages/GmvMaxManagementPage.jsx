import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import { useParams, useNavigate } from 'react-router-dom';

import FormField from '../../../../components/ui/FormField.jsx';
import {
  fetchBindingConfig,
  fetchGmvOptions,
  fetchSyncRun,
  fetchBusinessCenters,
  fetchAdvertisers,
  fetchStores,
  fetchProducts,
  listBindings,
  normProvider,
  saveBindingConfig,
  triggerProductSync,
} from '../service.js';
import {
  buildAdvertiserOptions,
  buildBusinessCenterOptions,
  buildStoreOptions,
} from '../utils/options.js';
import ProductCard from '../components/ProductCard.jsx';
import {
  selectGmvMaxScope,
  selectGmvMaxUI,
  selectProductsByKey,
  selectFetchStateByKey,
  setScope,
  setFilter,
  setSort,
  upsertProducts,
  setFetchState,
} from '../state/gmvMaxSlice.js';

const PRODUCTS_TTL_MS = 5 * 60 * 1000;
const FETCH_DEBOUNCE_MS = 300;

const DEFAULT_SCOPE = {
  wid: '',
  authId: '',
  bcId: '',
  advertiserId: '',
  storeId: '',
};

const DEFAULT_FILTER = {
  keyword: '',
  onlyAvailable: false,
  onlyUnoccupied: false,
  eligibility: 'gmv_max',
};

function extractErrorMessage(error, fallback = '操作失败') {
  if (!error) return fallback;
  const raw = error.message || (typeof error === 'string' ? error : '') || fallback;
  const cleaned = raw.startsWith('Error: ') ? raw.slice(7) : raw;
  try {
    const parsed = JSON.parse(cleaned);
    if (parsed?.error?.message) return parsed.error.message;
    if (parsed?.detail) return parsed.detail;
  } catch (_) {}
  return cleaned || fallback;
}

function formatTimestamp(isoString) {
  if (!isoString) return null;
  try {
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime())) return isoString;
    return date.toLocaleString('zh-CN', { hour12: false });
  } catch (error) {
    return typeof isoString === 'string' ? isoString : null;
  }
}

function formatSyncSummaryText(summary) {
  if (!summary || typeof summary !== 'object') {
    if (summary === null || summary === undefined) return '';
    return String(summary);
  }
  const hasCounters = ['added', 'removed', 'unchanged'].some((key) => key in summary);
  if (hasCounters) {
    const added = Number(summary.added ?? 0);
    const removed = Number(summary.removed ?? 0);
    const unchanged = Number(summary.unchanged ?? 0);
    return `新增 ${added} / 减少 ${removed} / 不变 ${unchanged}`;
  }
  const parts = Object.entries(summary)
    .map(([key, value]) => {
      const nested = formatSyncSummaryText(value);
      if (!nested) return key;
      return `${key}：${nested}`;
    })
    .filter(Boolean);
  return parts.join('；');
}

function formatOptionLabel(name, id) {
  const label = typeof name === 'string' ? name.trim() : '';
  const identifier = id ? String(id) : '';
  if (label && identifier) return `${label}（${identifier}）`;
  if (label) return label;
  if (identifier) return identifier;
  return '未命名';
}

function resolveBusinessCenterName(item) {
  if (!item) return '';
  return (
    (typeof item.name === 'string' && item.name.trim())
    || (item.raw?.bc_info?.name ? String(item.raw.bc_info.name).trim() : '')
    || (item.raw?.name ? String(item.raw.name).trim() : '')
    || (item.bc_id ? String(item.bc_id) : '')
  );
}

function resolveAdvertiserName(item) {
  if (!item) return '';
  return (
    (typeof item.display_name === 'string' && item.display_name.trim())
    || (typeof item.name === 'string' && item.name.trim())
    || (item.raw?.advertiser_name ? String(item.raw.advertiser_name).trim() : '')
    || (item.raw?.name ? String(item.raw.name).trim() : '')
    || (item.advertiser_id ? String(item.advertiser_id) : '')
  );
}

function resolveStoreName(item) {
  if (!item) return '';
  return (
    (typeof item.name === 'string' && item.name.trim())
    || (item.store_code ? String(item.store_code).trim() : '')
    || (item.store_id ? String(item.store_id) : '')
  );
}

function resolveProductTitle(item) {
  if (!item) return '';
  const title =
    (typeof item.title === 'string' && item.title.trim())
    || (item.raw?.title ? String(item.raw.title).trim() : '')
    || (item.product_id ? String(item.product_id) : '');
  return title;
}

function describeRunStatus(run) {
  if (!run) return { state: 'unknown', message: '同步任务已提交，正在等待状态更新。' };
  const status = String(run.status || '').toLowerCase();
  const runTag = run.id ? `（运行 #${run.id}）` : '';
  switch (status) {
    case 'pending':
    case 'scheduled':
    case 'queued':
      return { state: 'queued', message: `同步任务已排队${runTag}，请稍候。` };
    case 'running':
      return { state: 'running', message: `同步任务正在执行${runTag}。` };
    case 'succeeded':
      return { state: 'succeeded', message: `同步任务完成${runTag}。` };
    case 'failed': {
      const detailParts = [];
      if (run.error_code) detailParts.push(`错误码：${run.error_code}`);
      if (run.error_message) detailParts.push(run.error_message);
      return { state: 'failed', message: `同步任务失败${runTag}。`, detail: detailParts.join('；') || null };
    }
    case 'canceled':
    case 'cancelled':
      return { state: 'canceled', message: `同步任务已取消${runTag}。` };
    default:
      if (!status) return { state: 'unknown', message: `同步任务状态未知${runTag}。` };
      return { state: status, message: `同步任务状态：${run.status}${runTag}。` };
  }
}

function SummaryPanel({ summary }) {
  if (!summary) return null;
  return (
    <div className="card" style={{ display: 'grid', gap: '12px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' }}>
        <div>
          <h3 style={{ margin: 0 }}>最近一次刷新摘要</h3>
          <div className="small-muted">仅展示新增 / 减少 / 保持数量，详细数据请前往后台核对</div>
        </div>
      </div>
      <div className="summary-grid">
        {[
          { key: 'bc', label: 'Business Centers' },
          { key: 'advertisers', label: 'Advertisers' },
          { key: 'stores', label: 'Stores' },
        ].map((section) => {
          const data = summary?.[section.key] || {};
          return (
            <div key={section.key} className="summary-card">
              <div className="summary-card__title">{section.label}</div>
              <div className="summary-card__counts">
                <span>新增 <strong>{Number(data.added || 0)}</strong></span>
                <span>减少 <strong>{Number(data.removed || 0)}</strong></span>
                <span>不变 <strong>{Number(data.unchanged || 0)}</strong></span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function getProductSortValue(item, sortBy) {
  switch (sortBy) {
    case 'min_price':
      return Number(item?.min_price ?? item?.price_min ?? 0);
    case 'max_price':
      return Number(item?.max_price ?? item?.price_max ?? 0);
    case 'historical_sales':
      return Number(item?.historical_sales ?? item?.sales ?? 0);
    case 'updated_time': {
      const timestamp = item?.updated_time || item?.ext_updated_time;
      const value = timestamp ? Date.parse(timestamp) : NaN;
      return Number.isNaN(value) ? 0 : value;
    }
    default:
      return 0;
  }
}

export default function GmvMaxManagementPage() {
  const { wid } = useParams();
  const navigate = useNavigate();
  const provider = useMemo(() => normProvider(), []);

  const dispatch = useDispatch();
  const scope = useSelector(selectGmvMaxScope);
  const ui = useSelector(selectGmvMaxUI);
  const productsByKey = useSelector(selectProductsByKey);
  const fetchStateByKey = useSelector(selectFetchStateByKey);

  const productsRef = useRef(productsByKey);
  useEffect(() => { productsRef.current = productsByKey; }, [productsByKey]);

  useEffect(() => {
    if (wid && scope.wid !== wid) {
      dispatch(setScope({ ...DEFAULT_SCOPE, wid }));
    }
  }, [wid, scope.wid, dispatch]);

  const [bindings, setBindings] = useState([]);
  const [bindingsLoading, setBindingsLoading] = useState(false);
  const [bindingConfig, setBindingConfig] = useState(null);
  const [configVersion, setConfigVersion] = useState(0);

  const [businessCenters, setBusinessCenters] = useState([]);
  const [allAdvertisers, setAllAdvertisers] = useState([]);
  const [visibleAdvertisers, setVisibleAdvertisers] = useState([]);
  const [storesByAdvertiser, setStoresByAdvertiser] = useState({});
  const [advertisersLoading, setAdvertisersLoading] = useState(false);
  const [loadingStores, setLoadingStores] = useState(false);

  const [optionsEtag, setOptionsEtag] = useState(null);
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [refreshingOptions, setRefreshingOptions] = useState(false);

  const [triggeringSync, setTriggeringSync] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [metaSummary, setMetaSummary] = useState(null);
  const [syncRunStatus, setSyncRunStatus] = useState(null);
  const [autoSyncProducts, setAutoSyncProducts] = useState(false);

  const [fetchTick, setFetchTick] = useState(0);
  const triggerProductsFetch = useCallback(() => setFetchTick((prev) => prev + 1), []);

  const filter = ui?.filter || DEFAULT_FILTER;
  const sortBy = ui?.sortBy || 'min_price';
  const sortDir = ui?.sortDir || 'asc';

  const keyEligibility = filter.eligibility || 'gmv_max';
  const productKey = useMemo(() => {
    if (!wid || !scope?.authId || !scope?.storeId) return null;
    const bcId = scope.bcId || '';
    const advertiserId = scope.advertiserId || '';
    return `${wid}:${scope.authId}:${bcId}:${advertiserId}:${scope.storeId}:elig=${keyEligibility}`;
  }, [wid, scope?.authId, scope?.bcId, scope?.advertiserId, scope?.storeId, keyEligibility]);

  const cachedProducts = productKey ? productsByKey[productKey] : null;
  const fetchState = productKey ? fetchStateByKey[productKey] : null;

  const cachedAt = cachedProducts?.cachedAt ? Date.parse(cachedProducts.cachedAt) : NaN;

  useEffect(() => {
    let ignore = false;
    if (!wid) {
      setBindings([]);
      dispatch(setScope({ ...DEFAULT_SCOPE, wid: '' }));
      return () => { ignore = true; };
    }
    setBindingsLoading(true);
    setFeedback(null);
    listBindings(wid)
      .then((items) => {
        if (ignore) return;
        setBindings(Array.isArray(items) ? items : []);
      })
      .catch((error) => {
        if (ignore) return;
        setBindings([]);
        setFeedback({ type: 'error', text: extractErrorMessage(error, '无法获取绑定列表') });
      })
      .finally(() => { if (!ignore) setBindingsLoading(false); });
    return () => { ignore = true; };
  }, [wid, dispatch]);

  useEffect(() => {
    if (!bindings.length) {
      if (scope.authId) dispatch(setScope({ authId: '', bcId: '', advertiserId: '', storeId: '' }));
      return;
    }
    if (scope.authId && bindings.some((item) => String(item.auth_id) === String(scope.authId))) {
      return;
    }
    const first = bindings[0];
    if (first) {
      dispatch(setScope({ authId: String(first.auth_id || '') }));
    }
  }, [bindings, scope.authId, dispatch]);

  useEffect(() => {
    if (!wid || !scope.authId) {
      setBindingConfig(null);
      setAutoSyncProducts(false);
      return;
    }
    let ignore = false;
    setBindingConfig(null);
    (async () => {
      try {
        const config = await fetchBindingConfig(wid, provider, scope.authId);
        if (ignore) return;
        setBindingConfig(config || null);
        setAutoSyncProducts(Boolean(config?.auto_sync_products));
        dispatch(setScope({
          bcId: scope.bcId || (config?.bc_id ? String(config.bc_id) : ''),
          advertiserId: scope.advertiserId || (config?.advertiser_id ? String(config.advertiser_id) : ''),
          storeId: scope.storeId || (config?.store_id ? String(config.store_id) : ''),
        }));
      } catch (error) {
        if (!ignore) {
          setBindingConfig(null);
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 GMV Max 配置') });
        }
      }
    })();
    return () => { ignore = true; };
  }, [wid, provider, scope.authId, dispatch, configVersion]);

  useEffect(() => {
    if (!wid || !scope.authId) {
      setOptionsEtag(null);
      setMetaSummary(null);
      return;
    }
    let ignore = false;
    const controller = new AbortController();
    let pending = 2;
    setOptionsLoading(true);
    const finish = () => { if (!ignore && --pending <= 0) setOptionsLoading(false); };

    fetchGmvOptions(wid, provider, scope.authId, { signal: controller.signal, etag: optionsEtag })
      .then(({ status, data, etag }) => {
        if (ignore) return;
        if (status === 200 && data) {
          setMetaSummary(data.summary || null);
          setOptionsEtag(etag || null);
          const { refresh: refreshStatus, idempotency_key: refreshKey } = data || {};
          if (refreshStatus === 'timeout' && refreshKey) {
            setFeedback({ type: 'info', text: `已触发刷新，稍后再试（幂等键 ${refreshKey}）。` });
          }
        } else if (status === 304) {
          setOptionsEtag(etag || optionsEtag || null);
        }
      })
      .catch((error) => {
        if (!ignore) {
          setOptionsEtag(null);
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 GMV Max 选项') });
        }
      })
      .finally(finish);

    Promise.resolve()
      .then(async () => {
        const [bcResponse, advertiserResponse] = await Promise.all([
          fetchBusinessCenters(wid, provider, scope.authId),
          fetchAdvertisers(wid, provider, scope.authId),
        ]);
        if (ignore) return;
        setBusinessCenters(Array.isArray(bcResponse?.items) ? bcResponse.items : []);
        const advItems = Array.isArray(advertiserResponse?.items) ? advertiserResponse.items : [];
        setAllAdvertisers(advItems);
        setVisibleAdvertisers((prev) => (scope.bcId ? prev : advItems));
      })
      .catch((error) => {
        if (!ignore) {
          setBusinessCenters([]);
          setAllAdvertisers([]);
          setVisibleAdvertisers([]);
          setStoresByAdvertiser({});
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 TikTok 元数据') });
        }
      })
      .finally(finish);

    return () => { ignore = true; controller.abort(); };
  }, [wid, provider, scope.authId]);

  useEffect(() => {
    if (!wid || !scope.authId) {
      setAdvertisersLoading(false);
      setVisibleAdvertisers([]);
      return;
    }
    let ignore = false;
    setAdvertisersLoading(true);
    const params = scope.bcId ? { owner_bc_id: scope.bcId } : {};
    fetchAdvertisers(wid, provider, scope.authId, params)
      .then((response) => {
        if (ignore) return;
        const items = Array.isArray(response?.items) ? response.items : [];
        setVisibleAdvertisers(items);
        setStoresByAdvertiser((prev) => {
          if (!scope.bcId) return prev;
          const allowed = new Set(items.map((item) => String(item?.advertiser_id || '')));
          const next = {};
          Object.entries(prev || {}).forEach(([advId, stores]) => {
            if (allowed.has(String(advId))) next[advId] = stores;
          });
          return next;
        });
      })
      .catch((error) => {
        if (!ignore) {
          setVisibleAdvertisers([]);
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 Advertiser 列表') });
        }
      })
      .finally(() => { if (!ignore) setAdvertisersLoading(false); });
    return () => { ignore = true; };
  }, [wid, provider, scope.authId, scope.bcId]);

  useEffect(() => {
    if (!wid || !scope.authId || !scope.advertiserId) {
      setLoadingStores(false);
      return;
    }
    const existing = storesByAdvertiser[scope.advertiserId];
    if (existing) {
      const hasSelected = existing.some((item) => String(item?.store_id || '') === String(scope.storeId));
      if (!hasSelected && scope.storeId) {
        dispatch(setScope({ storeId: '' }));
      }
      return;
    }
    let ignore = false;
    setLoadingStores(true);
    fetchStores(wid, provider, scope.authId, scope.advertiserId, { owner_bc_id: scope.bcId || undefined })
      .then((response) => {
        if (ignore) return;
        const storeItems = Array.isArray(response?.items) ? response.items : [];
        setStoresByAdvertiser((prev) => ({ ...prev, [scope.advertiserId]: storeItems }));
        if (!storeItems.some((item) => String(item?.store_id || '') === String(scope.storeId))) {
          dispatch(setScope({ storeId: '' }));
        }
      })
      .catch((error) => {
        if (!ignore) {
          setStoresByAdvertiser((prev) => ({ ...prev, [scope.advertiserId]: [] }));
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 Store 列表') });
        }
      })
      .finally(() => { if (!ignore) setLoadingStores(false); });
    return () => { ignore = true; };
  }, [wid, provider, scope.authId, scope.advertiserId, scope.storeId, scope.bcId, storesByAdvertiser, dispatch]);

  const fetchControllerRef = useRef(null);
  const currentFetchRef = useRef(null);

  useEffect(() => {
    if (!productKey || !wid || !scope.authId || !scope.storeId) return () => {};
    let cancelled = false;
    const timer = setTimeout(() => {
      if (cancelled) return;
      if (fetchControllerRef.current) fetchControllerRef.current.abort();
      const controller = new AbortController();
      fetchControllerRef.current = controller;
      const requestId = `${productKey}:${Date.now()}`;
      currentFetchRef.current = { key: productKey, requestId };
      const cached = productsRef.current?.[productKey];
      const etag = cached?.etag || null;
      dispatch(setFetchState({ key: productKey, status: 'loading', error: null, requestId }));
      fetchProducts(
        wid,
        provider,
        scope.authId,
        scope.storeId,
        { eligibility: keyEligibility },
        { signal: controller.signal, etag }
      )
        .then((result) => {
          if (controller.signal.aborted || cancelled) return;
          if (!currentFetchRef.current || currentFetchRef.current.requestId !== requestId) return;
          if (result.status === 304) {
            dispatch(setFetchState({ key: productKey, status: 'succeeded', error: null, requestId }));
          } else if (result.status === 200 && result.data) {
            const items = Array.isArray(result.data.items) ? result.data.items : [];
            const pageSizeRaw = result.data.page_size ?? result.data.pageSize ?? items.length;
            const payload = {
              items,
              total: result.data.total ?? items.length,
              page: result.data.page ?? 1,
              pageSize: pageSizeRaw || 10,
              etag: result.etag || null,
              cachedAt: new Date().toISOString(),
            };
            dispatch(upsertProducts({ key: productKey, payload }));
            if (items.length === 0) {
              setFeedback({ type: 'info', text: '未找到 GMV Max 商品。' });
            } else {
              setFeedback({ type: 'success', text: `已拉取 ${payload.total} 条 GMV Max 商品。` });
            }
          } else {
            dispatch(setFetchState({ key: productKey, status: 'idle', error: null, requestId }));
          }
        })
        .catch((error) => {
          if (controller.signal.aborted || cancelled) return;
          if (!currentFetchRef.current || currentFetchRef.current.requestId !== requestId) return;
          const message = extractErrorMessage(error, '拉取 GMV Max 商品失败，请稍后重试');
          dispatch(setFetchState({ key: productKey, status: 'failed', error: message, requestId }));
          setFeedback({ type: 'error', text: message });
        })
        .finally(() => {
          if (!cancelled && fetchControllerRef.current === controller) {
            fetchControllerRef.current = null;
          }
        });
    }, FETCH_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [productKey, wid, provider, scope.authId, scope.storeId, keyEligibility, fetchTick, dispatch]);

  useEffect(() => {
    if (!productKey || !cachedProducts?.cachedAt) return undefined;
    const expiresIn = PRODUCTS_TTL_MS - (Date.now() - Date.parse(cachedProducts.cachedAt));
    if (expiresIn <= 0) {
      triggerProductsFetch();
      return undefined;
    }
    const timer = setTimeout(() => {
      triggerProductsFetch();
    }, expiresIn);
    return () => clearTimeout(timer);
  }, [productKey, cachedProducts?.cachedAt, triggerProductsFetch]);

  const handleBindingChange = useCallback((event) => {
    const value = event.target.value;
    dispatch(setScope({ authId: value, bcId: '', advertiserId: '', storeId: '' }));
    setStoresByAdvertiser({});
  }, [dispatch]);

  const handleBcChange = useCallback((event) => {
    const value = event.target.value;
    dispatch(setScope({ bcId: value, advertiserId: '', storeId: '' }));
    setStoresByAdvertiser({});
  }, [dispatch]);

  const handleAdvertiserChange = useCallback((event) => {
    const value = event.target.value;
    dispatch(setScope({ advertiserId: value, storeId: '' }));
  }, [dispatch]);

  const handleStoreChange = useCallback((event) => {
    dispatch(setScope({ storeId: event.target.value }));
  }, [dispatch]);

  const handleKeywordChange = useCallback((event) => {
    dispatch(setFilter({ keyword: event.target.value }));
  }, [dispatch]);

  const handleAvailableChange = useCallback((event) => {
    dispatch(setFilter({ onlyAvailable: event.target.checked }));
  }, [dispatch]);

  const handleUnoccupiedChange = useCallback((event) => {
    dispatch(setFilter({ onlyUnoccupied: event.target.checked }));
  }, [dispatch]);

  const handleSortByChange = useCallback((event) => {
    dispatch(setSort({ sortBy: event.target.value }));
  }, [dispatch]);

  const handleSortDirChange = useCallback((event) => {
    dispatch(setSort({ sortDir: event.target.value }));
  }, [dispatch]);

  const handleEligibilityChange = useCallback((event) => {
    dispatch(setFilter({ eligibility: event.target.value }));
    triggerProductsFetch();
  }, [dispatch, triggerProductsFetch]);

  const handleAutoSyncToggle = useCallback((event) => {
    setAutoSyncProducts(event.target.checked);
  }, []);

  const handleRefreshOptions = useCallback(async () => {
    if (!wid || !scope.authId) return;
    setRefreshingOptions(true);
    try {
      const { status, data, etag } = await fetchGmvOptions(wid, provider, scope.authId, { refresh: true, etag: optionsEtag });
      if (status === 200 && data) {
        setMetaSummary(data.summary || null);
        setOptionsEtag(etag || null);
        setFeedback({ type: 'success', text: '已刷新 GMV Max 选项。' });
      } else if (status === 304) {
        setFeedback({ type: 'info', text: '选项未更新，继续使用缓存。' });
      }
    } catch (error) {
      setFeedback({ type: 'error', text: extractErrorMessage(error, '刷新失败，请稍后再试。') });
    } finally {
      setRefreshingOptions(false);
    }
  }, [wid, provider, scope.authId, optionsEtag]);

  const handleSave = useCallback(async () => {
    if (!wid || !scope.authId) return;
    if (!scope.bcId || !scope.advertiserId || !scope.storeId) {
      setFeedback({ type: 'error', text: '请选择完整的 Business Center / Advertiser / Store。' });
      return;
    }
    setSaving(true);
    try {
      await saveBindingConfig(wid, provider, scope.authId, {
        bc_id: scope.bcId,
        advertiser_id: scope.advertiserId,
        store_id: scope.storeId,
        auto_sync_products: autoSyncProducts,
      });
      setFeedback({ type: 'success', text: '绑定配置已保存。' });
      setConfigVersion((prev) => prev + 1);
    } catch (error) {
      setFeedback({ type: 'error', text: extractErrorMessage(error, '保存失败，请稍后重试。') });
    } finally {
      setSaving(false);
    }
  }, [wid, provider, scope.authId, scope.bcId, scope.advertiserId, scope.storeId, autoSyncProducts]);

  const handlePullProducts = useCallback(() => {
    if (!wid || !scope.authId || !scope.storeId) {
      setFeedback({ type: 'error', text: '请选择 Store 后再拉取商品。' });
      return;
    }
    triggerProductsFetch();
  }, [wid, scope.authId, scope.storeId, triggerProductsFetch]);

  const handleProductSync = useCallback(async () => {
    if (!wid || !scope.authId) return;
    if (!scope.advertiserId || !scope.storeId) {
      setFeedback({ type: 'error', text: '请选择 Advertiser 与 Store 后再同步。' });
      return;
    }
    setTriggeringSync(true);
    setSyncRunStatus(null);
    try {
      const response = await triggerProductSync(wid, provider, scope.authId, {
        advertiser_id: scope.advertiserId,
        store_id: scope.storeId,
        eligibility: keyEligibility,
      });
      if (response?.run_id) {
        const runId = response.run_id;
        setFeedback({ type: 'success', text: `已提交同步任务（运行 #${runId}）。` });
        try {
          const status = await fetchSyncRun(wid, provider, scope.authId, runId);
          if (status) setSyncRunStatus(describeRunStatus(status));
        } catch (statusError) {
          const fallbackMessage = '已提交同步任务，但暂未获取最新状态。';
          const statusMessage = extractErrorMessage(statusError, fallbackMessage);
          setSyncRunStatus({
            state: 'pending',
            message: `同步任务已提交（运行 #${runId}），暂未获取最新状态。`,
            detail: statusMessage && statusMessage !== fallbackMessage ? statusMessage : null,
            runId,
          });
        }
      } else if (response?.status === 'succeeded') {
        setConfigVersion((prev) => prev + 1);
      }
    } catch (error) {
      let text;
      if (error?.status === 409) text = '同步进行中，请稍后再试。';
      else if (error?.status === 429) text = '同步过于频繁，请稍后再试。';
      else text = extractErrorMessage(error, '同步失败，请稍后再试。');
      setFeedback({ type: 'error', text });
      setSyncRunStatus({ state: 'error', message: text });
    } finally {
      setTriggeringSync(false);
    }
  }, [wid, provider, scope.authId, scope.advertiserId, scope.storeId, keyEligibility]);

  const handleOpenDetail = useCallback((product) => {
    if (!product || !wid) return;
    const targetId = product?.item_group_id || product?.product_id || product?.id;
    navigate(`/tenants/${wid}/gmv-max/products/${encodeURIComponent(targetId)}`, { state: { product } });
  }, [navigate, wid]);

  const optionsData = useMemo(() => {
    const safeBc = Array.isArray(businessCenters) ? businessCenters : [];
    const safeAdvertisers = Array.isArray(visibleAdvertisers) ? visibleAdvertisers : [];
    const safeAllAdvertisers = Array.isArray(allAdvertisers) ? allAdvertisers : safeAdvertisers;
    const storeGroups = Object.values(storesByAdvertiser || {});
    const stores = storeGroups.flat().filter(Boolean);
    const bcLinks = {};
    safeAllAdvertisers.forEach((item) => {
      const bcId = item?.bc_id ? String(item.bc_id) : '';
      const advId = item?.advertiser_id ? String(item.advertiser_id) : '';
      if (!bcId || !advId) return;
      if (!bcLinks[bcId]) bcLinks[bcId] = [];
      bcLinks[bcId].push(advId);
    });
    const advertiserStoreLinks = {};
    Object.entries(storesByAdvertiser || {}).forEach(([advId, list]) => {
      advertiserStoreLinks[advId] = (Array.isArray(list) ? list : [])
        .map((store) => (store?.store_id ? String(store.store_id) : null))
        .filter(Boolean);
    });
    return {
      bcs: safeBc,
      advertisers: safeAdvertisers,
      allAdvertisers: safeAllAdvertisers,
      stores,
      links: { bc_to_advertisers: bcLinks, advertiser_to_stores: advertiserStoreLinks },
    };
  }, [businessCenters, visibleAdvertisers, allAdvertisers, storesByAdvertiser]);

  const bcOptions = useMemo(() => buildBusinessCenterOptions(optionsData, scope.bcId), [optionsData, scope.bcId]);
  const advertiserOptions = useMemo(() => buildAdvertiserOptions(optionsData, scope.bcId, scope.advertiserId), [optionsData, scope.bcId, scope.advertiserId]);
  const storeOptions = useMemo(() => buildStoreOptions(optionsData, scope.advertiserId, scope.storeId, scope.bcId), [optionsData, scope.advertiserId, scope.storeId, scope.bcId]);

  const productItems = useMemo(() => {
    const items = Array.isArray(cachedProducts?.items) ? cachedProducts.items : [];
    const keyword = filter.keyword?.trim().toLowerCase();
    return items
      .filter((item) => {
        if (!item) return false;
        if (filter.onlyAvailable && String(item?.status || '').toUpperCase() !== 'AVAILABLE') return false;
        if (filter.onlyUnoccupied && String(item?.gmv_max_ads_status || '').toUpperCase() !== 'UNOCCUPIED') return false;
        if (keyword) {
          const title = resolveProductTitle(item).toLowerCase();
          const pid = String(item?.item_group_id || item?.product_id || '').toLowerCase();
          if (!title.includes(keyword) && !pid.includes(keyword)) return false;
        }
        return true;
      })
      .slice()
      .sort((a, b) => {
        const valueA = getProductSortValue(a, sortBy);
        const valueB = getProductSortValue(b, sortBy);
        if (valueA === valueB) return 0;
        return sortDir === 'asc' ? valueA - valueB : valueB - valueA;
      });
  }, [cachedProducts?.items, filter.keyword, filter.onlyAvailable, filter.onlyUnoccupied, sortBy, sortDir]);

  const isLoadingProducts = fetchState?.status === 'loading';
  const isRefreshingWithCache = isLoadingProducts && Array.isArray(cachedProducts?.items) && cachedProducts.items.length > 0;
  const fetchError = fetchState?.status === 'failed' ? fetchState.error : null;

  const disableRefresh = !scope.authId || refreshingOptions || optionsLoading;
  const disableSave = !scope.authId || saving || optionsLoading || advertisersLoading || loadingStores
    || !scope.bcId || !scope.advertiserId || !scope.storeId;
  const disableManualSync = !scope.authId || triggeringSync || optionsLoading || advertisersLoading || loadingStores
    || !scope.bcId || !scope.advertiserId || !scope.storeId;
  const disablePullProducts = !scope.authId || optionsLoading || advertisersLoading || !scope.storeId || loadingStores;

  const lastManualSyncedAt = formatTimestamp(bindingConfig?.last_manual_synced_at);
  const lastManualSummaryText = formatSyncSummaryText(bindingConfig?.last_manual_sync_summary);
  const lastAutoSyncedAt = formatTimestamp(bindingConfig?.last_auto_synced_at);
  const lastAutoSummaryText = formatSyncSummaryText(bindingConfig?.last_auto_sync_summary);

  return (
    <div className="gmv-max-page page-container">
      <div className="page-header">
        <div>
          <h1>GMV Max 管理</h1>
          <p className="small-muted">绑定后可配置 Business Center / Advertiser / Store，并触发元数据刷新。</p>
        </div>
      </div>

      {feedback?.text && (
        <div className={`alert ${feedback.type === 'error' ? 'alert--error' : feedback.type === 'success' ? 'alert--success' : ''}`}>
          {feedback.text}
        </div>
      )}
      {fetchError && (
        <div className="alert alert--error">{fetchError}</div>
      )}

      <div className="card gmv-toolbar">
        <div className="gmv-toolbar__selectors">
          <div className="gmv-toolbar__field">
            <FormField label="Binding (Alias)">
              <select className="form-input" value={scope.authId} onChange={handleBindingChange} disabled={bindingsLoading}>
                {!bindings.length && <option value="">暂无可用绑定</option>}
                {bindings.length > 0 && <option value="">请选择绑定</option>}
                {bindings.map((item) => {
                  const value = String(item.auth_id);
                  const alias = item.alias?.trim();
                  const label = alias ? `${alias}（#${value}）` : `授权 #${value}`;
                  return <option key={value} value={value}>{label}</option>;
                })}
              </select>
            </FormField>
          </div>

          <div className="gmv-toolbar__field">
            <FormField label="Business Center">
              <select className="form-input" value={scope.bcId} onChange={handleBcChange} disabled={!scope.authId || loadingStores || optionsLoading}>
                <option value="">请选择 Business Center</option>
                {bcOptions.map((item, idx) => {
                  const value = item?.bc_id ? String(item.bc_id) : '';
                  const label = formatOptionLabel(resolveBusinessCenterName(item), value);
                  return <option key={value || `missing-bc-${idx}`} value={value}>{label}</option>;
                })}
              </select>
            </FormField>
          </div>

          <div className="gmv-toolbar__field">
            <FormField label="Advertiser">
              <select className="form-input" value={scope.advertiserId} onChange={handleAdvertiserChange} disabled={!scope.authId || optionsLoading || advertisersLoading}>
                <option value="">请选择 Advertiser</option>
                {advertiserOptions.map((item, idx) => {
                  const value = item?.advertiser_id ? String(item.advertiser_id) : '';
                  const label = formatOptionLabel(resolveAdvertiserName(item), value);
                  return <option key={value || `missing-adv-${idx}`} value={value}>{label}</option>;
                })}
              </select>
            </FormField>
          </div>

          <div className="gmv-toolbar__field">
            <FormField label="Store">
              <select className="form-input" value={scope.storeId} onChange={handleStoreChange} disabled={!scope.authId || !scope.advertiserId || optionsLoading || loadingStores}>
                <option value="">请选择 Store</option>
                {storeOptions.map((item, idx) => {
                  const value = item?.store_id ? String(item.store_id) : '';
                  const baseLabel = formatOptionLabel(resolveStoreName(item), value);
                  const normalizedOwner = scope.bcId ? String(scope.bcId) : '';
                  let label = baseLabel;
                  if (normalizedOwner) {
                    const authorizedCandidates = [item?.store_authorized_bc_id, item?.bc_id, item?.bc_id_hint]
                      .map((candidate) => (candidate === undefined || candidate === null) ? '' : String(candidate))
                      .filter(Boolean);
                    const isAuthorized = authorizedCandidates.some((candidate) => candidate === normalizedOwner);
                    if (!isAuthorized) label = `${baseLabel}（未确认授权）`;
                  }
                  return <option key={value || `missing-store-${idx}`} value={value}>{label}</option>;
                })}
              </select>
            </FormField>
          </div>

          <label className="gmv-toolbar__autosync">
            <span>自动同步</span>
            <input type="checkbox" checked={autoSyncProducts} onChange={handleAutoSyncToggle} disabled={!scope.authId} />
          </label>
        </div>

        <div className="gmv-toolbar__actions">
          <button className="btn ghost" type="button" onClick={handleRefreshOptions} disabled={disableRefresh}>
            {refreshingOptions ? '刷新中…' : '刷新/同步'}
          </button>
          <button className="btn" type="button" onClick={handleSave} disabled={disableSave}>
            {saving ? '保存中…' : '保存激活组合'}
          </button>
          <button className="btn" type="button" onClick={handlePullProducts} disabled={disablePullProducts}>
            {isLoadingProducts ? '拉取中…' : '拉取 GMV Max 商品'}
          </button>
          <button className="btn" type="button" onClick={handleProductSync} disabled={disableManualSync}>
            {triggeringSync ? '同步中…' : '同步 GMV Max 商品'}
          </button>
        </div>
      </div>

      <div className="gmv-toolbar__meta small-muted">
        <div>最近一次手动同步：{lastManualSyncedAt || '暂无记录'}{lastManualSummaryText ? `（${lastManualSummaryText}）` : ''}</div>
        <div>最近一次自动同步：{lastAutoSyncedAt || '暂无记录'}{lastAutoSummaryText ? `（${lastAutoSummaryText}）` : ''}</div>
        {syncRunStatus?.message && (
          <div className={syncRunStatus.state === 'failed' || syncRunStatus.state === 'error' ? 'error-text' : ''}>
            {syncRunStatus.message}{syncRunStatus.detail ? `（${syncRunStatus.detail}）` : ''}
          </div>
        )}
      </div>

      <div className="card gmv-filters">
        <div className="gmv-filters__keyword">
          <input
            type="search"
            className="form-input"
            placeholder="搜索标题或商品 ID"
            value={filter.keyword || ''}
            onChange={handleKeywordChange}
          />
        </div>
        <label className="gmv-filters__checkbox">
          <input type="checkbox" checked={filter.onlyAvailable} onChange={handleAvailableChange} /> 仅可投放
        </label>
        <label className="gmv-filters__checkbox">
          <input type="checkbox" checked={filter.onlyUnoccupied} onChange={handleUnoccupiedChange} /> 仅未占用
        </label>
        <div className="gmv-filters__select">
          <select className="form-input" value={filter.eligibility} onChange={handleEligibilityChange}>
            <option value="gmv_max">GMV Max</option>
            <option value="gmv">GMV</option>
            <option value="all">全部</option>
          </select>
        </div>
        <div className="gmv-filters__sort">
          <select className="form-input" value={sortBy} onChange={handleSortByChange}>
            <option value="min_price">价格下限</option>
            <option value="max_price">价格上限</option>
            <option value="historical_sales">历史销量</option>
            <option value="updated_time">更新时间</option>
          </select>
          <select className="form-input" value={sortDir} onChange={handleSortDirChange}>
            <option value="asc">升序</option>
            <option value="desc">降序</option>
          </select>
        </div>
      </div>

      <div className="card gmv-products">
        <div className="gmv-products__header">
          <h3>GMV Max 商品</h3>
          <div className="small-muted">当前共 {cachedProducts?.total ?? 0} 条</div>
        </div>

        {isRefreshingWithCache && (
          <div className="small-muted">使用缓存渲染，后台校验中…</div>
        )}

        {!productItems.length ? (
          <div className="empty-state">
            {isLoadingProducts ? '正在拉取 GMV Max 商品…' : (scope.storeId ? '暂无商品数据，尝试刷新或调整过滤条件。' : '请选择 Store 并点击“拉取 GMV Max 商品”。')}
          </div>
        ) : (
          <div className="gmv-products__grid">
            {productItems.map((item) => {
              const key = item?.item_group_id || item?.product_id || item?.id;
              return (
                <ProductCard
                  key={key}
                  product={item}
                  onClick={handleOpenDetail}
                />
              );
            })}
          </div>
        )}
      </div>

      <SummaryPanel summary={metaSummary} />
    </div>
  );
}
