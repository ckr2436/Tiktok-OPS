
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';

import CopyButton from '../../../../../components/CopyButton.jsx';
import useDebouncedValue from '../../../../../utils/useDebouncedValue.js';
import {
  getSyncRun,
  listEntities,
  listProviderAccounts,
  normProvider,
  triggerSync,
} from '../service.js';
import {
  deriveShopBcDisplay,
  extractAdvertiserName,
  extractBcName,
  extractProductCurrency,
  extractProductTitle,
  extractShopName,
  formatDateTime,
  formatPrice,
  formatStock,
  mergeOptions,
  safeText,
} from '../utils/accountOverview.js';

const PAGE_SIZE_OPTIONS = [10, 20, 50];
const ELIGIBILITY_OPTIONS = [
  { value: 'all', label: 'All (默认)' },
  { value: 'gmv_max', label: 'GMV Max' },
  { value: 'ads', label: 'Ads' },
];
const RUN_TERMINAL = new Set(['success', 'failed']);
const RUN_POLL_INTERVAL = 2000;
const RUN_TIMEOUT_MS = 180000;

function isNextDisabled(page, pageSize, total, length) {
  const totalNum = Number(total);
  if (Number.isFinite(totalNum) && totalNum > 0) {
    return page * pageSize >= totalNum;
  }
  return length < pageSize;
}

function storageKey(wid, provider, authId) {
  return `ttb:last-run:${wid}:${provider}:${authId}`;
}

function Skeleton({ rows = 3 }) {
  return (
    <div className="skeleton-list">
      {Array.from({ length: rows }).map((_, idx) => (
        <div key={idx} className="skeleton-line" />
      ))}
    </div>
  );
}

function PanelHeader({ title, count, collapsed, onToggle }) {
  return (
    <button className="overview-panel__header" type="button" onClick={onToggle}>
      <div className="overview-panel__title">
        <span>{title}</span>
        <span className="overview-panel__count">({count})</span>
      </div>
      <span className={`overview-panel__chevron ${collapsed ? '' : 'open'}`}>⌄</span>
    </button>
  );
}

function ErrorState({ message, onRetry }) {
  return (
    <div className="overview-state overview-state--error">
      <div className="overview-state__message">{message || '加载失败'}</div>
      {onRetry && (
        <button className="btn ghost" onClick={onRetry} type="button">
          重试
        </button>
      )}
    </div>
  );
}

function EmptyState({ message, hints }) {
  return (
    <div className="overview-state overview-state--empty">
      <div className="overview-state__message">{message}</div>
      {Array.isArray(hints) && hints.length > 0 && (
        <ul className="overview-hints">
          {hints.map((hint, idx) => (
            <li key={idx}>{hint}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ProductEligibilityGuard({ active }) {
  if (!active) return null;
  return (
    <div className="overview-warning">
      需要选择 Advertiser 才能按资格过滤。请先在上方选择 Advertiser，或将 Eligibility 设为 All。
    </div>
  );
}

function ProductEligibilityNotice({ visible }) {
  if (!visible) return null;
  return (
    <div className="overview-guard">
      需要选择 Advertiser 才能按资格过滤。请先在上方选择 Advertiser，或将 Eligibility 设为 All。
    </div>
  );
}

function Toast({ toast, onDismiss }) {
  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => onDismiss(), toast.duration ?? 2600);
    return () => clearTimeout(timer);
  }, [toast, onDismiss]);

  if (!toast) return null;
  return (
    <div className={`toast toast--${toast.tone || 'info'}`} role="status">
      {toast.message}
    </div>
  );
}

function SyncProgressModal({ open, runId, wid, provider, authId, onClose, onComplete }) {
  const [run, setRun] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const completedRef = useRef(false);

  useEffect(() => {
    if (!open || !runId) return undefined;
    let active = true;
    let timer = null;
    const startedAt = Date.now();

    async function fetchRun() {
      if (!active) return;
      setLoading(true);
      try {
        const data = await getSyncRun(wid, provider, authId, runId);
        if (!active) return;
        setRun(data);
        setError('');
        const status = String(data?.status || '').toLowerCase();
        const elapsed = Date.now() - startedAt;
        const timedOut = elapsed >= RUN_TIMEOUT_MS && !RUN_TERMINAL.has(status);
        const terminal = RUN_TERMINAL.has(status) || timedOut;
        if (timedOut) {
          setError('轮询超时');
        }
        if (terminal && !completedRef.current) {
          completedRef.current = true;
          onComplete?.(timedOut ? 'timeout' : status, data);
        }
        if (!terminal) {
          timer = setTimeout(fetchRun, RUN_POLL_INTERVAL);
        }
      } catch (err) {
        if (!active) return;
        setError(err?.message || '获取运行状态失败');
        timer = setTimeout(fetchRun, RUN_POLL_INTERVAL);
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }

    fetchRun();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [open, runId, wid, provider, authId, onComplete]);

  useEffect(() => {
    if (!open) {
      setRun(null);
      setError('');
      completedRef.current = false;
    }
  }, [open]);

  if (!open || !runId) return null;

  const status = String(run?.status || '').toLowerCase();
  const stats = run?.stats || {};
  const counts = stats?.processed?.counts || {};
  const errors = stats?.errors || run?.errors || [];

  return (
    <div className="modal-backdrop">
      <div className="modal modal--wide">
        <div className="modal__header">
          <div className="modal__title">同步进度 #{runId}</div>
          <button className="modal__close" onClick={onClose} type="button">
            关闭
          </button>
        </div>
        <div className="modal__body space-y-4">
          <div className="overview-run__summary">
            <div>状态：{status || '-'}</div>
            <div>任务：{safeText(run?.task_name)}</div>
            <div>耗时：{run?.duration_ms != null ? `${run.duration_ms} ms` : '-'}</div>
            <div>排队时间：{formatDateTime(run?.enqueued_at)}</div>
            <div>计划时间：{formatDateTime(run?.scheduled_for)}</div>
          </div>
          {loading && <div className="small-muted">刷新中…</div>}
          {error && <div className="alert alert--error">{error}</div>}
          <div className="overview-run__counts">
            {Object.entries(counts).map(([scope, row]) => (
              <div key={scope} className="overview-run__card">
                <div className="overview-run__scope">{scope}</div>
                <div className="overview-run__numbers">
                  <span>fetched：{row?.fetched ?? 0}</span>
                  <span>upserts：{row?.upserts ?? 0}</span>
                  <span>skipped：{row?.skipped ?? 0}</span>
                </div>
              </div>
            ))}
            {Object.keys(counts).length === 0 && (
              <div className="overview-state__message small-muted">暂无统计</div>
            )}
          </div>
          {errors.length > 0 && (
            <div className="overview-run__errors">
              <div className="text-base font-semibold">错误</div>
              {errors.map((item, idx) => (
                <div key={idx} className="alert alert--error">
                  <div>阶段：{safeText(item?.stage)}</div>
                  <div>错误码：{safeText(item?.code)}</div>
                  <div>说明：{safeText(item?.message)}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function AccountOverviewPage() {
  const { wid, authId } = useParams();
  const provider = useMemo(() => normProvider(), []);

  const [account, setAccount] = useState(null);
  const [accountLoading, setAccountLoading] = useState(true);

  const [filters, setFilters] = useState({
    bcId: '',
    advertiserId: '',
    shopId: '',
    eligibility: 'all',
  });
  const debouncedFilters = useDebouncedValue(filters, 300);

  const [toast, setToast] = useState(null);

  const [bcPage, setBcPage] = useState(1);
  const [bcPageSize, setBcPageSize] = useState(10);
  const [bcItems, setBcItems] = useState([]);
  const [bcTotal, setBcTotal] = useState(0);
  const [bcLoading, setBcLoading] = useState(false);
  const [bcError, setBcError] = useState('');
  const [bcCollapsed, setBcCollapsed] = useState(true);
  const [bcOptions, setBcOptions] = useState([]);

  const [advPage, setAdvPage] = useState(1);
  const [advPageSize, setAdvPageSize] = useState(10);
  const [advItems, setAdvItems] = useState([]);
  const [advTotal, setAdvTotal] = useState(0);
  const [advLoading, setAdvLoading] = useState(false);
  const [advError, setAdvError] = useState('');
  const [advCollapsed, setAdvCollapsed] = useState(true);
  const [advOptions, setAdvOptions] = useState([]);

  const [shopPage, setShopPage] = useState(1);
  const [shopPageSize, setShopPageSize] = useState(10);
  const [shopItems, setShopItems] = useState([]);
  const [shopTotal, setShopTotal] = useState(0);
  const [shopLoading, setShopLoading] = useState(false);
  const [shopError, setShopError] = useState('');
  const [shopCollapsed, setShopCollapsed] = useState(false);
  const [shopOptions, setShopOptions] = useState([]);

  const [productPage, setProductPage] = useState(1);
  const [productPageSize, setProductPageSize] = useState(10);
  const [productItems, setProductItems] = useState([]);
  const [productTotal, setProductTotal] = useState(0);
  const [productLoading, setProductLoading] = useState(false);
  const [productError, setProductError] = useState('');
  const [productCollapsed, setProductCollapsed] = useState(false);

  const [refreshVersion, setRefreshVersion] = useState({ bc: 0, adv: 0, shop: 0, product: 0 });
  const [productGuardActive, setProductGuardActive] = useState(false);
  const [syncScope, setSyncScope] = useState('all');
  const [syncLoading, setSyncLoading] = useState(false);
  const [progressModal, setProgressModal] = useState({ open: false, runId: null, scope: 'all' });
  const [lastRunId, setLastRunId] = useState(null);

  const bcAbortRef = useRef(null);
  const advAbortRef = useRef(null);
  const shopAbortRef = useRef(null);
  const productAbortRef = useRef(null);
  const syncScopeRef = useRef('all');

  useEffect(() => {
    let active = true;
    setAccountLoading(true);
    async function loadAccount() {
      try {
        let page = 1;
        const pageSize = 50;
        let found = null;
        while (active) {
          const data = await listProviderAccounts(wid, provider, { page, page_size: pageSize });
          const items = Array.isArray(data?.items) ? data.items : [];
          found = items.find((item) => String(item?.auth_id) === String(authId));
          if (found || items.length === 0) break;
          const total = Number(data?.total || 0);
          if (page * pageSize >= total) break;
          page += 1;
        }
        if (!active) return;
        setAccount(found || null);
        setAccountLoading(false);
        if (!found) {
          setToast({ message: '未找到账号信息', tone: 'warn' });
        }
      } catch (err) {
        if (!active) return;
        setAccount(null);
        setAccountLoading(false);
        setToast({ message: err?.message || '获取账号信息失败', tone: 'error', duration: 3600 });
      }
    }
    loadAccount();
    return () => {
      active = false;
    };
  }, [wid, provider, authId]);

  useEffect(() => () => {
    bcAbortRef.current?.abort();
    advAbortRef.current?.abort();
    shopAbortRef.current?.abort();
    productAbortRef.current?.abort();
  }, []);

  useEffect(() => {
    try {
      const stored = window.localStorage?.getItem(storageKey(wid, provider, authId));
      if (stored) {
        const parsed = Number.parseInt(stored, 10);
        setLastRunId(Number.isFinite(parsed) ? parsed : stored);
      }
    } catch (err) {
      // ignore storage errors
    }
  }, [wid, provider, authId]);

  const handleFilterChange = useCallback((name, value) => {
    setFilters((prev) => {
      if (name === 'bcId') {
        setAdvPage(1);
        setShopPage(1);
        setProductPage(1);
        return {
          bcId: value,
          advertiserId: '',
          shopId: '',
          eligibility: prev.eligibility,
        };
      }
      if (name === 'advertiserId') {
        setShopPage(1);
        setProductPage(1);
        return {
          ...prev,
          advertiserId: value,
          shopId: '',
        };
      }
      if (name === 'shopId') {
        setProductPage(1);
        return { ...prev, shopId: value };
      }
      if (name === 'eligibility') {
        setProductPage(1);
        return { ...prev, eligibility: value || 'all' };
      }
      return prev;
    });
  }, []);

  const handleResetFilters = useCallback(() => {
    setFilters({ bcId: '', advertiserId: '', shopId: '', eligibility: 'all' });
    setBcPage(1);
    setAdvPage(1);
    setShopPage(1);
    setProductPage(1);
  }, []);

  const handleCopyError = useCallback(() => {
    setToast({ message: '复制失败，请稍后重试', tone: 'error' });
  }, []);

  const handleCopySuccess = useCallback(() => {
    setToast({ message: '内容已复制到剪贴板', tone: 'success' });
  }, []);

  const refreshByScope = useCallback((scope) => {
    const key = String(scope || 'all').toLowerCase();
    const targets = new Set();
    if (key === 'products') {
      targets.add('product');
    } else if (key === 'shops') {
      targets.add('shop');
      targets.add('product');
    } else if (key === 'advertisers') {
      targets.add('adv');
      targets.add('shop');
      targets.add('product');
    } else if (key === 'business-centers' || key === 'business_centers' || key === 'bc') {
      targets.add('bc');
    } else {
      targets.add('bc');
      targets.add('adv');
      targets.add('shop');
      targets.add('product');
    }
    setRefreshVersion((prev) => ({
      bc: prev.bc + (targets.has('bc') ? 1 : 0),
      adv: prev.adv + (targets.has('adv') ? 1 : 0),
      shop: prev.shop + (targets.has('shop') ? 1 : 0),
      product: prev.product + (targets.has('product') ? 1 : 0),
    }));
  }, []);

  const loadBusinessCenters = useCallback(
    async (page, pageSize) => {
      if (bcAbortRef.current) {
        bcAbortRef.current.abort();
      }
      const controller = new AbortController();
      bcAbortRef.current = controller;
      setBcLoading(true);
      setBcError('');
      try {
        const data = await listEntities(
          wid,
          provider,
          authId,
          'business-centers',
          {
            page,
            page_size: pageSize,
          },
          { signal: controller.signal }
        );
        if (bcAbortRef.current !== controller) return;
        const items = Array.isArray(data?.items) ? data.items : [];
        setBcItems(items);
        setBcTotal(Number(data?.total || 0));
        const valid = items.filter((row) => row?.bc_id);
        if (valid.length > 0) {
          setBcOptions((prev) => mergeOptions(prev, valid, (row) => String(row?.bc_id)));
        }
      } catch (err) {
        if (bcAbortRef.current !== controller) return;
        if (err?.name === 'AbortError') return;
        setBcItems([]);
        setBcTotal(0);
        setBcError(err?.message || '加载失败');
      } finally {
        if (bcAbortRef.current === controller) {
          setBcLoading(false);
          bcAbortRef.current = null;
        }
      }
    },
    [wid, provider, authId]
  );

  const loadAdvertisers = useCallback(
    async (page, pageSize, bcId) => {
      if (advAbortRef.current) {
        advAbortRef.current.abort();
      }
      const controller = new AbortController();
      advAbortRef.current = controller;
      setAdvLoading(true);
      setAdvError('');
      try {
        const params = { page, page_size: pageSize };
        if (bcId) params.bc_id = bcId;
        const data = await listEntities(
          wid,
          provider,
          authId,
          'advertisers',
          params,
          { signal: controller.signal }
        );
        if (advAbortRef.current !== controller) return;
        const items = Array.isArray(data?.items) ? data.items : [];
        setAdvItems(items);
        setAdvTotal(Number(data?.total || 0));
        const valid = items.filter((row) => row?.advertiser_id);
        if (valid.length > 0) {
          setAdvOptions((prev) => mergeOptions(prev, valid, (row) => String(row?.advertiser_id)));
        }
      } catch (err) {
        if (advAbortRef.current !== controller) return;
        if (err?.name === 'AbortError') return;
        setAdvItems([]);
        setAdvTotal(0);
        setAdvError(err?.message || '加载失败');
      } finally {
        if (advAbortRef.current === controller) {
          setAdvLoading(false);
          advAbortRef.current = null;
        }
      }
    },
    [wid, provider, authId]
  );

  const loadShops = useCallback(
    async (page, pageSize, advertiserId) => {
      if (shopAbortRef.current) {
        shopAbortRef.current.abort();
      }
      const controller = new AbortController();
      shopAbortRef.current = controller;
      setShopLoading(true);
      setShopError('');
      try {
        const params = { page, page_size: pageSize };
        if (advertiserId) params.advertiser_id = advertiserId;
        const data = await listEntities(wid, provider, authId, 'shops', params, {
          signal: controller.signal,
        });
        if (shopAbortRef.current !== controller) return;
        const items = Array.isArray(data?.items) ? data.items : [];
        setShopItems(items);
        setShopTotal(Number(data?.total || 0));
        const valid = items.filter((row) => row?.shop_id);
        if (valid.length > 0) {
          setShopOptions((prev) => mergeOptions(prev, valid, (row) => String(row?.shop_id)));
        }
      } catch (err) {
        if (shopAbortRef.current !== controller) return;
        if (err?.name === 'AbortError') return;
        setShopItems([]);
        setShopTotal(0);
        setShopError(err?.message || '加载失败');
      } finally {
        if (shopAbortRef.current === controller) {
          setShopLoading(false);
          shopAbortRef.current = null;
        }
      }
    },
    [wid, provider, authId]
  );

  const loadProducts = useCallback(
    async (page, pageSize, shopId, eligibility, advertiserId) => {
      if (productAbortRef.current) {
        productAbortRef.current.abort();
      }
      const controller = new AbortController();
      productAbortRef.current = controller;
      setProductLoading(true);
      setProductError('');
      try {
        const params = { page, page_size: pageSize };
        if (shopId) {
          params.shop_id = shopId;
          params.store_id = shopId;
        }
        if (eligibility && eligibility !== 'all') params.eligibility = eligibility;
        if (eligibility && eligibility !== 'all' && advertiserId) {
          params.advertiser_id = advertiserId;
        }
        const data = await listEntities(wid, provider, authId, 'products', params, {
          signal: controller.signal,
        });
        if (productAbortRef.current !== controller) return;
        const items = Array.isArray(data?.items) ? data.items : [];
        setProductItems(items);
        setProductTotal(Number(data?.total || 0));
      } catch (err) {
        if (productAbortRef.current !== controller) return;
        if (err?.name === 'AbortError') return;
        setProductItems([]);
        setProductTotal(0);
        setProductError(err?.message || '加载失败');
      } finally {
        if (productAbortRef.current === controller) {
          setProductLoading(false);
          productAbortRef.current = null;
        }
      }
    },
    [wid, provider, authId]
  );

  useEffect(() => {
    loadBusinessCenters(bcPage, bcPageSize);
  }, [loadBusinessCenters, bcPage, bcPageSize, refreshVersion.bc]);

  useEffect(() => {
    loadAdvertisers(advPage, advPageSize, debouncedFilters.bcId);
  }, [loadAdvertisers, advPage, advPageSize, debouncedFilters.bcId, refreshVersion.adv]);

  useEffect(() => {
    loadShops(shopPage, shopPageSize, debouncedFilters.advertiserId);
  }, [loadShops, shopPage, shopPageSize, debouncedFilters.advertiserId, refreshVersion.shop]);

  useEffect(() => {
    const requiresAdvertiser =
      ['ads', 'gmv_max'].includes(debouncedFilters.eligibility) && !debouncedFilters.advertiserId;
    if (requiresAdvertiser) {
      setProductGuardActive(true);
      setProductLoading(false);
      setProductError('');
      setProductItems([]);
      setProductTotal(0);
      if (productAbortRef.current) {
        productAbortRef.current.abort();
        productAbortRef.current = null;
      }
      return;
    }
    setProductGuardActive(false);
    loadProducts(
      productPage,
      productPageSize,
      debouncedFilters.shopId,
      debouncedFilters.eligibility,
      debouncedFilters.advertiserId
    );
  }, [
    loadProducts,
    productPage,
    productPageSize,
    debouncedFilters.shopId,
    debouncedFilters.eligibility,
    debouncedFilters.advertiserId,
    refreshVersion.product,
  ]);

  const bcSelectOptions = useMemo(
    () =>
      bcOptions.map((row) => ({
        value: String(row.bc_id),
        label: `${row.bc_id} · ${extractBcName(row)}`,
      })),
    [bcOptions]
  );

  const advertiserSelectOptions = useMemo(() => {
    const currentBc = filters.bcId ? String(filters.bcId) : '';
    return advOptions
      .filter((row) => !currentBc || String(row?.bc_id) === currentBc)
      .map((row) => ({
        value: String(row.advertiser_id),
        label: `${row.advertiser_id} · ${extractAdvertiserName(row)}`,
      }));
  }, [advOptions, filters.bcId]);

  const shopSelectOptions = useMemo(() => {
    const currentAdv = filters.advertiserId ? String(filters.advertiserId) : '';
    return shopOptions
      .filter((row) => !currentAdv || String(row?.advertiser_id) === currentAdv)
      .map((row) => ({
        value: String(row.shop_id),
        label: `${row.shop_id} · ${extractShopName(row)}`,
      }));
  }, [shopOptions, filters.advertiserId]);

  const guardEligibility = useMemo(
    () => ['ads', 'gmv_max'].includes(filters.eligibility) && !filters.advertiserId,
    [filters.eligibility, filters.advertiserId]
  );

  const handleSync = useCallback(async () => {
    if (guardEligibility && syncScope === 'products') {
      setToast({ message: '选择 GMV/ADS 时需先指定 Advertiser', tone: 'warn' });
      return;
    }
    syncScopeRef.current = syncScope;
    setSyncLoading(true);
    try {
      const payload = { mode: 'incremental' };
      if (filters.shopId) payload.shop_id = filters.shopId;
      if (
        filters.eligibility &&
        filters.eligibility !== 'all' &&
        syncScope !== 'shops' &&
        filters.advertiserId
      ) {
        payload.product_eligibility = filters.eligibility;
      }
      const resp = await triggerSync(wid, provider, authId, syncScope, payload);
      const runId = resp?.run_id;
      if (runId) {
        try {
          window.localStorage?.setItem(storageKey(wid, provider, authId), String(runId));
        } catch (err) {
          // ignore
        }
        setLastRunId(runId);
      }
      setProgressModal({ open: true, runId: runId || null, scope: syncScope });
      setToast({ message: '已触发同步', tone: 'success' });
    } catch (err) {
      setToast({ message: err?.message || '同步触发失败', tone: 'error', duration: 3600 });
    } finally {
      setSyncLoading(false);
    }
  }, [guardEligibility, syncScope, filters.shopId, filters.eligibility, wid, provider, authId]);

  const handleViewLastRun = useCallback(() => {
    if (!lastRunId) {
      setToast({ message: '暂无同步记录', tone: 'warn' });
      return;
    }
    syncScopeRef.current = 'all';
    setProgressModal({ open: true, runId: lastRunId, scope: 'all' });
  }, [lastRunId]);

  const handleRunComplete = useCallback(
    (status) => {
      if (status === 'success') {
        setToast({ message: '同步完成', tone: 'success' });
        const scopeToRefresh = progressModal.scope || syncScopeRef.current || 'all';
        refreshByScope(scopeToRefresh);
      } else if (status === 'failed') {
        setToast({ message: '同步失败，请查看错误详情', tone: 'error', duration: 3600 });
      } else if (status === 'timeout') {
        setToast({ message: '同步状态查询超时，请稍后手动刷新', tone: 'warn', duration: 3600 });
      }
    },
    [progressModal.scope, refreshByScope]
  );

  const accountLabel = account?.label ? safeText(account.label) : '-';
  const accountStatus = account?.status ? safeText(account.status) : '-';

  const bcCount = Number(bcTotal) > 0 ? bcTotal : bcItems.length;
  const advCount = Number(advTotal) > 0 ? advTotal : advItems.length;
  const shopCount = Number(shopTotal) > 0 ? shopTotal : shopItems.length;
  const productCount = Number(productTotal) > 0 ? productTotal : productItems.length;

  const bcNextDisabled = isNextDisabled(bcPage, bcPageSize, bcTotal, bcItems.length);
  const advNextDisabled = isNextDisabled(advPage, advPageSize, advTotal, advItems.length);
  const shopNextDisabled = isNextDisabled(shopPage, shopPageSize, shopTotal, shopItems.length);
  const productNextDisabled =
    productGuardActive || isNextDisabled(productPage, productPageSize, productTotal, productItems.length);

  return (
    <div className="account-overview">
      <Toast toast={toast} onDismiss={() => setToast(null)} />
      <div className="overview-toolbar">
        <div className="overview-toolbar__meta">
          <div className="overview-toolbar__title">TTB Account Overview</div>
          <div className="overview-toolbar__info">
            <span>Alias：{accountLoading ? '加载中…' : accountLabel}</span>
            <span>auth_id：{authId}</span>
            <span>Provider：{provider}</span>
            <span>Status：{accountStatus}</span>
          </div>
        </div>
        <div className="overview-toolbar__filters">
          <label className="filter-field">
            <span className="filter-label">Business Center</span>
            <select
              className="form-input"
              value={filters.bcId}
              onChange={(e) => handleFilterChange('bcId', e.target.value)}
            >
              <option value="">全部</option>
              {bcSelectOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>
          <label className="filter-field">
            <span className="filter-label">Advertiser</span>
            <select
              className="form-input"
              value={filters.advertiserId}
              onChange={(e) => handleFilterChange('advertiserId', e.target.value)}
            >
              <option value="">全部</option>
              {advertiserSelectOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>
          <label className="filter-field">
            <span className="filter-label">Shop</span>
            <select
              className="form-input"
              value={filters.shopId}
              onChange={(e) => handleFilterChange('shopId', e.target.value)}
            >
              <option value="">全部</option>
              {shopSelectOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>
          <label className="filter-field">
            <span className="filter-label">Eligibility</span>
            <select
              className="form-input"
              value={filters.eligibility}
              onChange={(e) => handleFilterChange('eligibility', e.target.value)}
            >
              {ELIGIBILITY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>
          <ProductEligibilityGuard active={guardEligibility} />
        </div>
        <div className="overview-toolbar__actions">
          <button className="btn ghost" type="button" onClick={handleResetFilters}>
            重置筛选
          </button>
          <select
            className="form-input overview-toolbar__scope"
            value={syncScope}
            onChange={(e) => setSyncScope(e.target.value)}
          >
            <option value="all">同步全部</option>
            <option value="shops">仅店铺</option>
            <option value="products">仅商品</option>
          </select>
          <button
            className="btn"
            type="button"
            onClick={handleSync}
            disabled={syncLoading || (syncScope === 'products' && guardEligibility)}
          >
            {syncLoading ? '同步中…' : 'Sync Now'}
          </button>
          <button className="btn ghost" type="button" onClick={handleViewLastRun} disabled={!lastRunId}>
            查看最近一次 Run
          </button>
        </div>
      </div>

      <div className="overview-panels">
        <section className={`overview-panel ${bcCollapsed ? 'collapsed' : ''}`}>
          <PanelHeader
            title="Business Centers"
            count={bcCount}
            collapsed={bcCollapsed}
            onToggle={() => setBcCollapsed((prev) => !prev)}
          />
          {!bcCollapsed && (
            <div className="overview-panel__body">
              {bcLoading ? (
                <Skeleton rows={4} />
              ) : bcError ? (
                <ErrorState message={bcError} onRetry={() => loadBusinessCenters(bcPage, bcPageSize)} />
              ) : bcItems.length === 0 ? (
                <EmptyState message="暂无数据" hints={['请先点击 Sync Now 同步基础数据']} />
              ) : (
                <div className="overview-table-wrap">
                  <table className="overview-table">
                    <thead>
                      <tr>
                        <th>BC ID</th>
                        <th>名称</th>
                        <th>状态</th>
                        <th>时区</th>
                        <th>国家</th>
                        <th>更新时间</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {bcItems.map((row) => (
                        <tr key={row.bc_id}>
                          <td>{safeText(row.bc_id)}</td>
                          <td>{extractBcName(row)}</td>
                          <td>{safeText(row.status)}</td>
                          <td>{safeText(row.timezone)}</td>
                          <td>{safeText(row.country_code)}</td>
                          <td>{formatDateTime(row.ext_updated_time)}</td>
                          <td>
                            <CopyButton
                              text={row.bc_id}
                              onError={handleCopyError}
                              onSuccess={handleCopySuccess}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="overview-pagination">
                    <div>共 {bcTotal} 条 · 第 {bcPage}</div>
                    <div className="overview-pagination__controls">
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setBcPage((p) => Math.max(1, p - 1))}
                        disabled={bcPage <= 1}
                      >
                        上一页
                      </button>
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setBcPage((p) => p + 1)}
                        disabled={bcNextDisabled}
                      >
                        下一页
                      </button>
                      <select
                        className="form-input"
                        value={bcPageSize}
                        onChange={(e) => {
                          setBcPageSize(Number(e.target.value));
                          setBcPage(1);
                        }}
                      >
                        {PAGE_SIZE_OPTIONS.map((size) => (
                          <option key={size} value={size}>
                            {size}/页
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>

        <section className={`overview-panel ${advCollapsed ? 'collapsed' : ''}`}>
          <PanelHeader
            title="Advertisers"
            count={advCount}
            collapsed={advCollapsed}
            onToggle={() => setAdvCollapsed((prev) => !prev)}
          />
          {!advCollapsed && (
            <div className="overview-panel__body">
              {advLoading ? (
                <Skeleton rows={4} />
              ) : advError ? (
                <ErrorState message={advError} onRetry={() => loadAdvertisers(advPage, advPageSize, debouncedFilters.bcId)} />
              ) : advItems.length === 0 ? (
                <EmptyState message="暂无数据" hints={['请先点击 Sync Now 同步基础数据']} />
              ) : (
                <div className="overview-table-wrap">
                  <table className="overview-table">
                    <thead>
                      <tr>
                        <th>Advertiser ID</th>
                        <th>名称</th>
                        <th>状态</th>
                        <th>行业</th>
                        <th>货币</th>
                        <th>时区</th>
                        <th>国家</th>
                        <th>更新时间</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {advItems.map((row) => (
                        <tr key={row.advertiser_id}>
                          <td>{safeText(row.advertiser_id)}</td>
                          <td>{extractAdvertiserName(row)}</td>
                          <td>{safeText(row.status)}</td>
                          <td>{safeText(row.industry)}</td>
                          <td>{safeText(row.currency)}</td>
                          <td>{safeText(row.timezone)}</td>
                          <td>{safeText(row.country_code)}</td>
                          <td>{formatDateTime(row.ext_updated_time)}</td>
                          <td>
                            <CopyButton
                              text={row.advertiser_id}
                              onError={handleCopyError}
                              onSuccess={handleCopySuccess}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="overview-pagination">
                    <div>共 {advTotal} 条 · 第 {advPage}</div>
                    <div className="overview-pagination__controls">
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setAdvPage((p) => Math.max(1, p - 1))}
                        disabled={advPage <= 1}
                      >
                        上一页
                      </button>
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setAdvPage((p) => p + 1)}
                        disabled={advNextDisabled}
                      >
                        下一页
                      </button>
                      <select
                        className="form-input"
                        value={advPageSize}
                        onChange={(e) => {
                          setAdvPageSize(Number(e.target.value));
                          setAdvPage(1);
                        }}
                      >
                        {PAGE_SIZE_OPTIONS.map((size) => (
                          <option key={size} value={size}>
                            {size}/页
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>

        <section className={`overview-panel ${shopCollapsed ? 'collapsed' : ''}`}>
          <PanelHeader
            title="Shops"
            count={shopCount}
            collapsed={shopCollapsed}
            onToggle={() => setShopCollapsed((prev) => !prev)}
          />
          {!shopCollapsed && (
            <div className="overview-panel__body">
              {shopLoading ? (
                <Skeleton rows={4} />
              ) : shopError ? (
                <ErrorState message={shopError} onRetry={() => loadShops(shopPage, shopPageSize, debouncedFilters.advertiserId)} />
              ) : shopItems.length === 0 ? (
                <EmptyState message="暂无数据" hints={['请先点击 Sync Now 同步基础数据']} />
              ) : (
                <div className="overview-table-wrap">
                  <table className="overview-table">
                    <thead>
                      <tr>
                        <th>Shop ID</th>
                        <th>名称</th>
                        <th>状态</th>
                        <th>区域</th>
                        <th>BC ID</th>
                        <th>Advertiser</th>
                        <th>更新时间</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {shopItems.map((row) => {
                        const bcDisplay = deriveShopBcDisplay(row);
                        return (
                          <tr key={row.shop_id}>
                            <td>{safeText(row.shop_id)}</td>
                            <td>{extractShopName(row)}</td>
                            <td>{safeText(row.status)}</td>
                            <td>{safeText(row.region_code)}</td>
                            <td>
                              <span>{bcDisplay.value}</span>
                              {bcDisplay.needsBackfill && (
                                <span className="badge badge--pending">待回填</span>
                              )}
                            </td>
                            <td>{safeText(row.advertiser_id)}</td>
                            <td>{formatDateTime(row.ext_updated_time)}</td>
                            <td>
                              <CopyButton
                                text={row.shop_id}
                                onError={handleCopyError}
                                onSuccess={handleCopySuccess}
                              />
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  <div className="overview-pagination">
                    <div>共 {shopTotal} 条 · 第 {shopPage}</div>
                    <div className="overview-pagination__controls">
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setShopPage((p) => Math.max(1, p - 1))}
                        disabled={shopPage <= 1}
                      >
                        上一页
                      </button>
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setShopPage((p) => p + 1)}
                        disabled={shopNextDisabled}
                      >
                        下一页
                      </button>
                      <select
                        className="form-input"
                        value={shopPageSize}
                        onChange={(e) => {
                          setShopPageSize(Number(e.target.value));
                          setShopPage(1);
                        }}
                      >
                        {PAGE_SIZE_OPTIONS.map((size) => (
                          <option key={size} value={size}>
                            {size}/页
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>

        <section className={`overview-panel ${productCollapsed ? 'collapsed' : ''}`}>
          <PanelHeader
            title="Products"
            count={productCount}
            collapsed={productCollapsed}
            onToggle={() => setProductCollapsed((prev) => !prev)}
          />
          {!productCollapsed && (
            <div className="overview-panel__body">
              <ProductEligibilityNotice visible={productGuardActive} />
              {productGuardActive ? (
                <EmptyState
                  message="暂无数据"
                  hints={['1) 选择具体 Shop', '2) 将 Eligibility 设为 All', '3) 完成同步后刷新']}
                />
              ) : productLoading ? (
                <Skeleton rows={4} />
              ) : productError ? (
                <ErrorState message={productError} onRetry={() =>
                  loadProducts(
                    productPage,
                    productPageSize,
                    debouncedFilters.shopId,
                    debouncedFilters.eligibility,
                    debouncedFilters.advertiserId
                  )
                } />
              ) : productItems.length === 0 ? (
                <EmptyState
                  message="暂无数据"
                  hints={[
                    '1) 选择具体 Shop',
                    '2) 将 Eligibility 设为 All',
                    '3) 完成同步后刷新',
                  ]}
                />
              ) : (
                <div className="overview-table-wrap">
                  <table className="overview-table">
                    <thead>
                      <tr>
                        <th>Product ID</th>
                        <th>标题</th>
                        <th>状态</th>
                        <th>价格</th>
                        <th>库存</th>
                        <th>货币</th>
                        <th>更新时间</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {productItems.map((row) => (
                        <tr key={row.product_id}>
                          <td>{safeText(row.product_id)}</td>
                          <td>{extractProductTitle(row)}</td>
                          <td>{safeText(row.status)}</td>
                          <td>{formatPrice(row)}</td>
                          <td>{formatStock(row)}</td>
                          <td>{extractProductCurrency(row)}</td>
                          <td>{formatDateTime(row.ext_updated_time)}</td>
                          <td>
                            <CopyButton
                              text={row.product_id}
                              onError={handleCopyError}
                              onSuccess={handleCopySuccess}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="overview-pagination">
                    <div>共 {productTotal} 条 · 第 {productPage}</div>
                    <div className="overview-pagination__controls">
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setProductPage((p) => Math.max(1, p - 1))}
                        disabled={productPage <= 1 || productGuardActive}
                      >
                        上一页
                      </button>
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setProductPage((p) => p + 1)}
                        disabled={productNextDisabled}
                      >
                        下一页
                      </button>
                      <select
                        className="form-input"
                        value={productPageSize}
                        onChange={(e) => {
                          setProductPageSize(Number(e.target.value));
                          setProductPage(1);
                        }}
                      >
                        {PAGE_SIZE_OPTIONS.map((size) => (
                          <option key={size} value={size}>
                            {size}/页
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>
      </div>

      <SyncProgressModal
        open={progressModal.open}
        runId={progressModal.runId}
        wid={wid}
        provider={provider}
        authId={authId}
        onClose={() => setProgressModal({ open: false, runId: null, scope: 'all' })}
        onComplete={handleRunComplete}
      />
    </div>
  );
}
