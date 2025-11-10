// src/features/tenants/gmv_max/pages/GmvMaxManagementPage.jsx
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams, useLocation, useNavigate } from 'react-router-dom';

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

/** ---------------------------
 * 本地持久化配置
 * --------------------------- */
const LS_KEY = 'gmv.max.v1';                 // 总键
const PRODUCTS_TTL_MS = 10 * 60 * 1000;      // 产品缓存 10 分钟过期
const SCOPE_TTL_MS = 24 * 60 * 60 * 1000;    // scope 记忆 1 天

function now() { return Date.now(); }

function readLS() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch { return {}; }
}
function writeLS(data) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(data || {})); } catch {}
}
function scopeBucketKey({ wid, authId, storeId }) {
  return `${wid || ''}/${authId || ''}/${storeId || ''}`;
}
function saveScopeToLS({ wid, authId, bcId, advertiserId, storeId }) {
  const db = readLS();
  db.__scope__ = db.__scope__ || {};
  db.__scope__[wid] = { authId, bcId, advertiserId, storeId, savedAt: now() };
  writeLS(db);
}
function readScopeFromLS(wid) {
  const db = readLS();
  const s = db.__scope__?.[wid];
  if (!s) return null;
  if (!s.savedAt || now() - s.savedAt > SCOPE_TTL_MS) return null;
  return s;
}
function saveProductsCache({ wid, authId, storeId, productsState }) {
  const db = readLS();
  db.__products__ = db.__products__ || {};
  const key = scopeBucketKey({ wid, authId, storeId });
  const normalized = normalizeProductsPayload(productsState, now());
  db.__products__[key] = {
    productsState: normalized,
    savedAt: normalized.receivedAt || now(),
  };
  writeLS(db);
}
function readProductsCache({ wid, authId, storeId }) {
  const db = readLS();
  const key = scopeBucketKey({ wid, authId, storeId });
  const rec = db.__products__?.[key];
  if (!rec) return null;
  if (!rec.savedAt || now() - rec.savedAt > PRODUCTS_TTL_MS) return null;
  const cached = rec.productsState || rec;
  const normalized = normalizeProductsPayload(cached, rec.savedAt);
  return normalized;
}

/** ---------------------------
 * 现有常量 / 工具
 * --------------------------- */
const DEFAULT_FORM = {
  bcId: '',
  advertiserId: '',
  storeId: '',
  autoSyncProducts: false,
};

const SUMMARY_SECTIONS = [
  { key: 'bc', label: 'Business Centers' },
  { key: 'advertisers', label: 'Advertisers' },
  { key: 'stores', label: 'Stores' },
];

const PRODUCT_POLL_INTERVAL_MS = 30 * 1000;

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
  } catch { return typeof isoString === 'string' ? isoString : null; }
}

function formatLocalTimeFromMillis(value) {
  if (value === null || value === undefined) return null;
  try {
    const date = new Date(Number(value));
    if (Number.isNaN(date.getTime())) return null;
    return date.toLocaleString('zh-CN', { hour12: false });
  } catch {
    return null;
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

function resolveItemGroupId(item) {
  if (!item || typeof item !== 'object') return '';
  const candidates = [
    item.item_group_id,
    item.item_groupid,
    item.itemGroupId,
    item.raw?.item_group_id,
    item.raw?.item_groupid,
  ];
  for (const candidate of candidates) {
    if (candidate === undefined || candidate === null) continue;
    const value = String(candidate).trim();
    if (value) return value;
  }
  return '';
}

function guessBooleanFromStatus(status) {
  if (status === null || status === undefined) return null;
  if (typeof status === 'boolean') return status;
  if (typeof status === 'number') {
    if (status === 1) return true;
    if (status === 0) return false;
  }
  const normalized = String(status).trim().toLowerCase();
  if (!normalized) return null;
  if (normalized === 'true') return true;
  if (normalized === 'false') return false;
  if (normalized === '1') return true;
  if (normalized === '0') return false;

  const negativeKeywords = [
    'inactive', 'unavailable', 'off_sale', 'off', 'offline', 'disabled', 'suspended',
    'deleted', 'sold_out', 'not_available', 'blocked', 'invalid', 'unbound', 'released',
  ];
  if (negativeKeywords.some((keyword) => normalized.includes(keyword))) return false;

  const positiveKeywords = [
    'available', 'on_sale', 'active', 'online', 'enabled', 'published', 'in_stock',
    'occupied', 'binding', 'bound', 'in_use', 'linked',
  ];
  if (positiveKeywords.some((keyword) => normalized.includes(keyword))) return true;

  return null;
}

function isProductAvailable(item) {
  if (!item || typeof item !== 'object') return false;
  if (item.is_available !== undefined) return Boolean(item.is_available);
  if (item.available !== undefined) return Boolean(item.available);
  if (item.availability?.is_available !== undefined) return Boolean(item.availability.is_available);
  if (item.inventory?.available !== undefined) return Boolean(item.inventory.available);

  const statusCandidates = [
    item.availability_status,
    item.availability,
    item.status,
    item.lifecycle_status,
    item.sale_status,
  ];

  for (const candidate of statusCandidates) {
    const guessed = guessBooleanFromStatus(candidate);
    if (guessed !== null) return guessed;
  }

  return true;
}

function describeAvailabilityStatus(item) {
  if (!item || typeof item !== 'object') return '';
  const textCandidates = [
    item.availability_status,
    item.status_text,
    item.status,
    item.lifecycle_status,
    item.sale_status,
    item.availability,
  ];

  for (const candidate of textCandidates) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
  }

  if (item.is_available === false || item.available === false) return '不可用';
  if (item.is_available === true || item.available === true) return '可用';

  return '';
}

function isOccupiedByGmvMax(item) {
  if (!item || typeof item !== 'object') return false;

  const booleanCandidates = [
    item.is_gmv_max_bound,
    item.gmv_max_bound,
    item.gmv_max_binding_active,
    item.gmv_max_binding?.is_active,
    item.gmv_max_binding?.active,
  ];
  for (const candidate of booleanCandidates) {
    if (candidate === undefined || candidate === null) continue;
    if (typeof candidate === 'boolean') return candidate;
    if (typeof candidate === 'number') return candidate === 1;
  }

  const statusCandidates = [
    item.gmv_max_binding_status,
    item.gmv_max_status,
    item.gmv_status,
    item.gmv_max_binding?.status,
    item.gmv_binding_status,
  ];
  for (const candidate of statusCandidates) {
    const guessed = guessBooleanFromStatus(candidate);
    if (guessed !== null) return guessed;
  }

  if (Array.isArray(item.gmv_max_bindings) && item.gmv_max_bindings.length > 0) return true;
  if (item.gmv_max_binding && Object.keys(item.gmv_max_binding).length > 0) return true;

  return false;
}

function extractProductImage(item) {
  if (!item || typeof item !== 'object') return '';
  const candidates = [];
  const push = (value) => {
    if (!value) return;
    if (typeof value === 'string' && value.trim()) candidates.push(value.trim());
    else if (typeof value === 'object' && value.url && typeof value.url === 'string' && value.url.trim()) candidates.push(value.url.trim());
  };

  push(item.image_url || item.imageUrl || item.image);
  if (Array.isArray(item.images)) item.images.forEach(push);
  if (Array.isArray(item.image_list)) item.image_list.forEach(push);
  if (Array.isArray(item.raw?.images)) item.raw.images.forEach(push);
  push(item.raw?.image_url || item.raw?.cover_image_url);
  push(item.cover_image?.url || item.main_image?.url || item.thumbnail_url);

  return candidates.find(Boolean) || '';
}

function normalizeProductsPayload(payload, receivedAtHint) {
  if (!payload) {
    return {
      items: [],
      availableCount: 0,
      totalFromApi: 0,
      rawItemsCount: 0,
      filteredOut: 0,
      page: 1,
      pageSize: 10,
      receivedAt: receivedAtHint || now(),
      __normalized: true,
    };
  }

  if (payload.__normalized) {
    return {
      ...payload,
      receivedAt: payload.receivedAt || receivedAtHint || now(),
    };
  }

  const rawItems = Array.isArray(payload.items) ? payload.items : [];
  const availableItems = rawItems.filter(isProductAvailable);
  const totalFromApi = Number.isFinite(Number(payload.total)) ? Number(payload.total) : rawItems.length;
  const page = Number.isFinite(Number(payload.page)) ? Number(payload.page) : 1;
  const pageSizeRaw = payload.page_size ?? payload.pageSize;
  const pageSize = Number.isFinite(Number(pageSizeRaw)) ? Number(pageSizeRaw) : Math.max(availableItems.length, 10);

  return {
    items: availableItems,
    availableCount: availableItems.length,
    totalFromApi,
    rawItemsCount: rawItems.length,
    filteredOut: rawItems.length - availableItems.length,
    page,
    pageSize,
    receivedAt: receivedAtHint || now(),
    __normalized: true,
  };
}
function describeRunStatus(run) {
  if (!run) return { state: 'unknown', message: '同步任务已提交，正在等待状态更新。' };
  const status = String(run.status || '').toLowerCase();
  const runTag = run.id ? `（运行 #${run.id}）` : '';
  switch (status) {
    case 'pending':
    case 'scheduled':
    case 'queued':   return { state: 'queued', message: `同步任务已排队${runTag}，请稍候。` };
    case 'running':  return { state: 'running', message: `同步任务正在执行${runTag}。` };
    case 'succeeded':return { state: 'succeeded', message: `同步任务完成${runTag}。` };
    case 'failed': {
      const detailParts = [];
      if (run.error_code) detailParts.push(`错误码：${run.error_code}`);
      if (run.error_message) detailParts.push(run.error_message);
      return { state: 'failed', message: `同步任务失败${runTag}。`, detail: detailParts.join('；') || null };
    }
    case 'canceled':
    case 'cancelled':return { state: 'canceled', message: `同步任务已取消${runTag}。` };
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

/** ---------------------------
 * 页面组件
 * --------------------------- */
export default function GmvMaxManagementPage() {
  const { wid } = useParams();
  const provider = useMemo(() => normProvider(), []);
  const location = useLocation();
  const navigate = useNavigate();

  /** URL <-> 状态 同步 */
  const qs = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const urlAuthId = qs.get('authId') || '';
  const urlBcId = qs.get('bcId') || '';
  const urlAdvId = qs.get('advertiserId') || '';
  const urlStoreId = qs.get('storeId') || '';

  const [selectedAuthId, setSelectedAuthId] = useState(urlAuthId || '');
  const [configVersion, setConfigVersion] = useState(0);

  const [form, setForm] = useState({
    ...DEFAULT_FORM,
    bcId: urlBcId || DEFAULT_FORM.bcId,
    advertiserId: urlAdvId || DEFAULT_FORM.advertiserId,
    storeId: urlStoreId || DEFAULT_FORM.storeId,
  });
  const formRef = useRef(DEFAULT_FORM);

  const queryClient = useQueryClient();

  const [storesByAdvertiser, setStoresByAdvertiser] = useState({});
  const [triggeringSync, setTriggeringSync] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [metaSummary, setMetaSummary] = useState(null);
  const [syncRunStatus, setSyncRunStatus] = useState(null);

  const optionsEtagRef = useRef(null);
  const productsAnnouncementRef = useRef(false);

  const pushErrorFeedback = useCallback((error, fallback) => {
    const text = extractErrorMessage(error, fallback);
    setFeedback((prev) => {
      if (prev?.type === 'error' && prev.text === text) return prev;
      return { type: 'error', text };
    });
  }, [setFeedback]);

  const bindingsQuery = useQuery({
    queryKey: ['gmv-max', 'bindings', wid],
    enabled: !!wid,
    queryFn: () => (wid ? listBindings(wid) : []),
    onError: (error) => pushErrorFeedback(error, '无法获取绑定列表'),
  });

  const bindings = useMemo(
    () => (Array.isArray(bindingsQuery.data) ? bindingsQuery.data : []),
    [bindingsQuery.data],
  );

  const optionsQueryKey = useMemo(
    () => ['gmv-max', 'options', wid, selectedAuthId],
    [wid, selectedAuthId],
  );

  const optionsQuery = useQuery({
    queryKey: optionsQueryKey,
    enabled: !!wid && !!selectedAuthId,
    queryFn: ({ signal }) => fetchGmvOptions(wid, provider, selectedAuthId, { etag: optionsEtagRef.current, signal }),
    onSuccess: (result) => {
      if (result?.etag !== undefined) optionsEtagRef.current = result.etag || null;
      if (result?.data?.summary !== undefined) setMetaSummary(result.data.summary || null);
    },
    onError: (error) => pushErrorFeedback(error, '无法加载 GMV Max 选项'),
  });

  const businessCentersQuery = useQuery({
    queryKey: ['gmv-max', 'business-centers', wid, selectedAuthId],
    enabled: !!wid && !!selectedAuthId,
    queryFn: ({ signal }) => fetchBusinessCenters(wid, provider, selectedAuthId, { signal }),
    select: (response) => (Array.isArray(response?.items) ? response.items : []),
    onError: (error) => pushErrorFeedback(error, '无法加载 TikTok 元数据'),
  });

  const advertisersAllQuery = useQuery({
    queryKey: ['gmv-max', 'advertisers', wid, selectedAuthId, 'all'],
    enabled: !!wid && !!selectedAuthId,
    queryFn: ({ signal }) => fetchAdvertisers(wid, provider, selectedAuthId, {}, { signal }),
    select: (response) => (Array.isArray(response?.items) ? response.items : []),
    onError: (error) => pushErrorFeedback(error, '无法加载 TikTok 元数据'),
  });

  const advertisersByBcQuery = useQuery({
    queryKey: ['gmv-max', 'advertisers', wid, selectedAuthId, 'bc', form.bcId || ''],
    enabled: !!wid && !!selectedAuthId && !!form.bcId,
    queryFn: ({ signal }) => fetchAdvertisers(
      wid,
      provider,
      selectedAuthId,
      { owner_bc_id: form.bcId },
      { signal },
    ),
    select: (response) => (Array.isArray(response?.items) ? response.items : []),
    onError: (error) => pushErrorFeedback(error, '无法加载 Advertiser 列表'),
  });

  const storesQuery = useQuery({
    queryKey: ['gmv-max', 'stores', wid, selectedAuthId, form.advertiserId || '', form.bcId || ''],
    enabled: !!wid && !!selectedAuthId && !!form.advertiserId,
    queryFn: ({ signal }) => fetchStores(
      wid,
      provider,
      selectedAuthId,
      form.advertiserId,
      form.bcId ? { owner_bc_id: form.bcId } : {},
      { signal },
    ),
    select: (response) => (Array.isArray(response?.items) ? response.items : []),
    onError: (error) => pushErrorFeedback(error, '无法加载 Store 列表'),
  });

  const bindingConfigQuery = useQuery({
    queryKey: ['gmv-max', 'binding-config', wid, selectedAuthId, configVersion],
    enabled: !!wid && !!selectedAuthId,
    queryFn: ({ signal }) => fetchBindingConfig(wid, provider, selectedAuthId, { signal }),
    select: (response) => response || null,
    onError: (error) => pushErrorFeedback(error, '无法加载 GMV Max 配置'),
  });

  const bindingConfig = bindingConfigQuery.data || null;

  const refreshOptionsMutation = useMutation({
    mutationFn: async () => {
      if (!wid || !selectedAuthId) {
        throw new Error('缺少必要的绑定信息');
      }
      return fetchGmvOptions(wid, provider, selectedAuthId, { refresh: true, etag: optionsEtagRef.current });
    },
    onSuccess: (result) => {
      if (result?.etag !== undefined) optionsEtagRef.current = result.etag || null;
      if (result?.data?.summary !== undefined) setMetaSummary(result.data.summary || null);

      if (result?.status === 304) {
        setFeedback({ type: 'info', text: '元数据未发生变化。' });
      } else if (result?.data) {
        const { refresh: refreshStatus, idempotency_key: refreshKey } = result.data || {};
        if (refreshStatus === 'timeout') {
          setFeedback({
            type: 'info',
            text: refreshKey
              ? `已触发刷新，稍后再试（幂等键 ${refreshKey}）。`
              : '已触发刷新，稍后再试。',
          });
        } else {
          setFeedback({
            type: 'success',
            text: refreshKey
              ? `已刷新最新可选项（幂等键 ${refreshKey}）。`
              : '已刷新最新可选项。',
          });
        }
      }

      if (wid && selectedAuthId) {
        queryClient.setQueryData(optionsQueryKey, result);
        queryClient.invalidateQueries({ queryKey: ['gmv-max', 'business-centers', wid, selectedAuthId] });
        queryClient.invalidateQueries({ queryKey: ['gmv-max', 'advertisers', wid, selectedAuthId] });
      }
    },
    onError: (error) => pushErrorFeedback(error, '刷新失败，请稍后重试'),
  });

  const productsQueryEnabled = !!wid && !!selectedAuthId && !!form.storeId;
  const productQueryKey = useMemo(
    () => ['gmv-max', 'products', wid || '', selectedAuthId || '', form.storeId || ''],
    [wid, selectedAuthId, form.storeId],
  );
  const initialProductsCache = useMemo(
    () => (productsQueryEnabled ? readProductsCache({ wid, authId: selectedAuthId, storeId: form.storeId }) : null),
    [productsQueryEnabled, wid, selectedAuthId, form.storeId],
  );

  const productsQuery = useQuery({
    queryKey: productQueryKey,
    enabled: productsQueryEnabled,
    queryFn: ({ signal }) => fetchProducts(
      wid,
      provider,
      selectedAuthId,
      form.storeId,
      {},
      { signal },
    ),
    initialData: initialProductsCache || undefined,
    keepPreviousData: true,
    staleTime: 0,
    refetchInterval: productsQueryEnabled ? PRODUCT_POLL_INTERVAL_MS : false,
    refetchOnWindowFocus: false,
    select: (payload) => normalizeProductsPayload(payload, Date.now()),
    onSuccess: (data) => {
      if (!wid || !selectedAuthId || !form.storeId) return;
      saveProductsCache({ wid, authId: selectedAuthId, storeId: form.storeId, productsState: data });
      if (productsAnnouncementRef.current) {
        if ((data?.availableCount || 0) === 0) {
          setFeedback({ type: 'info', text: '未找到 GMV Max 商品。' });
        } else {
          const filteredNote = data.filteredOut > 0
            ? `（已过滤 ${data.filteredOut} 条不可用商品）`
            : '';
          setFeedback({
            type: 'success',
            text: `已拉取 ${data.availableCount} 条可用 GMV Max 商品${filteredNote}。`,
          });
        }
        productsAnnouncementRef.current = false;
      }
    },
    onError: (error) => {
      if (productsAnnouncementRef.current) {
        setFeedback({ type: 'error', text: extractErrorMessage(error, '拉取 GMV Max 商品失败，请稍后重试') });
        productsAnnouncementRef.current = false;
      }
    },
  });

  const bindingsLoading = bindingsQuery.isLoading || bindingsQuery.isFetching;
  const optionsLoading = optionsQuery.isLoading || optionsQuery.isFetching;
  const businessCenters = businessCentersQuery.data || [];
  const allAdvertisers = advertisersAllQuery.data || [];
  const advertisersForBc = advertisersByBcQuery.data || [];
  const advertisersLoading = form.bcId
    ? (advertisersByBcQuery.isLoading || advertisersByBcQuery.isFetching)
    : (advertisersAllQuery.isLoading || advertisersAllQuery.isFetching);
  const visibleAdvertisers = form.bcId ? advertisersForBc : allAdvertisers;
  const stores = storesQuery.data || [];
  const loadingStores = storesQuery.isLoading || storesQuery.isFetching;
  const loadingConfig = bindingConfigQuery.isLoading;
  const refreshingOptions = refreshOptionsMutation.isPending;

  // 让 ref 时刻反映最新 form
  useEffect(() => { formRef.current = form; }, [form]);

  // URL 同步（把关键 scope 写回到地址栏，便于刷新/分享）
  const syncUrl = useCallback((next) => {
    const sp = new URLSearchParams(location.search);
    const { authId, bcId, advertiserId, storeId } = next;
    const setOrDel = (k, v) => { if (v) sp.set(k, v); else sp.delete(k); };
    setOrDel('authId', authId);
    setOrDel('bcId', bcId);
    setOrDel('advertiserId', advertiserId);
    setOrDel('storeId', storeId);
    navigate({ pathname: location.pathname, search: sp.toString() }, { replace: true });
  }, [location.pathname, location.search, navigate]);

  /** 绑定选中逻辑（默认选第一个；优先 URL / LS） */
  useEffect(() => {
    if (!wid) {
      setSelectedAuthId('');
      setMetaSummary(null);
      setForm(DEFAULT_FORM);
      setStoresByAdvertiser({});
      queryClient.removeQueries({ queryKey: ['gmv-max', 'products'], exact: false });
      productsAnnouncementRef.current = false;
      optionsEtagRef.current = null;
      return;
    }
    if (!bindings.length) {
      setSelectedAuthId('');
      setMetaSummary(null);
      setForm(DEFAULT_FORM);
      setStoresByAdvertiser({});
      queryClient.removeQueries({ queryKey: ['gmv-max', 'products'], exact: false });
      productsAnnouncementRef.current = false;
      optionsEtagRef.current = null;
      return;
    }
    setSelectedAuthId((prev) => {
      if (urlAuthId && bindings.some((b) => String(b.auth_id) === String(urlAuthId))) return urlAuthId;
      if (prev && bindings.some((item) => String(item.auth_id) === String(prev))) return prev;
      const saved = readScopeFromLS(wid);
      if (saved?.authId && bindings.some((b) => String(b.auth_id) === String(saved.authId))) return String(saved.authId);
      const first = bindings[0];
      return first ? String(first.auth_id) : '';
    });
  }, [bindings, queryClient, urlAuthId, wid]);

  /** 切换绑定后，恢复 scope（优先 URL -> LS -> 服务端配置） */
  useEffect(() => {
    setMetaSummary(null);
    setStoresByAdvertiser({});
    setSyncRunStatus(null);
    queryClient.removeQueries({ queryKey: ['gmv-max', 'products', wid || '', selectedAuthId || ''], exact: false });
    productsAnnouncementRef.current = false;
    optionsEtagRef.current = null;

    if (!wid || !selectedAuthId) {
      setForm(DEFAULT_FORM);
      return;
    }

    if (urlBcId || urlAdvId || urlStoreId) {
      setForm((prev) => ({
        ...prev,
        bcId: urlBcId || '',
        advertiserId: urlAdvId || '',
        storeId: urlStoreId || '',
      }));
      return;
    }

    const saved = readScopeFromLS(wid);
    if (saved?.authId && String(saved.authId) === String(selectedAuthId)) {
      setForm((prev) => ({
        ...prev,
        bcId: saved.bcId || '',
        advertiserId: saved.advertiserId || '',
        storeId: saved.storeId || '',
      }));
      return;
    }

    setForm((prev) => ({
      ...prev,
      bcId: '',
      advertiserId: '',
      storeId: '',
    }));
  }, [queryClient, selectedAuthId, wid, urlBcId, urlAdvId, urlStoreId]);

  /** 绑定配置（用于初次 scope 回填） */
  useEffect(() => {
    if (!bindingConfig || !selectedAuthId) return;
    setForm((prev) => ({
      ...prev,
      bcId: prev.bcId || (bindingConfig?.bc_id ?? ''),
      advertiserId: prev.advertiserId || (bindingConfig?.advertiser_id ?? ''),
      storeId: prev.storeId || (bindingConfig?.store_id ?? ''),
      autoSyncProducts: Boolean(bindingConfig?.auto_sync_products),
    }));
  }, [bindingConfig, selectedAuthId]);

  useEffect(() => {
    if (!form.bcId) return;
    const allowed = new Set(visibleAdvertisers.map((item) => String(item?.advertiser_id || '')));
    setStoresByAdvertiser((prev) => {
      const next = {};
      Object.entries(prev || {}).forEach(([advId, storesList]) => {
        if (allowed.has(String(advId))) next[advId] = storesList;
      });
      return next;
    });
  }, [form.bcId, visibleAdvertisers]);

  useEffect(() => {
    if (!form.advertiserId || !Array.isArray(stores)) return;
    setStoresByAdvertiser((prev) => ({
      ...prev,
      [form.advertiserId]: stores,
    }));
    if (form.storeId && !stores.some((item) => String(item?.store_id || '') === String(form.storeId))) {
      setForm((prev) => (prev.advertiserId === form.advertiserId ? { ...prev, storeId: '' } : prev));
    }
  }, [form.advertiserId, form.storeId, stores]);

  /** ---------- 事件处理：scope 变更 + URL/LS 同步 ---------- */
  function handleBindingChange(evt) {
    const v = evt.target.value || '';
    setSelectedAuthId(v);
    saveScopeToLS({ wid, authId: v, bcId: '', advertiserId: '', storeId: '' });
    syncUrl({ authId: v, bcId: '', advertiserId: '', storeId: '' });
    queryClient.removeQueries({ queryKey: ['gmv-max', 'products', wid || ''], exact: false });
    productsAnnouncementRef.current = false;
  }
  function handleBcChange(evt) {
    const value = evt.target.value || '';
    const next = { ...form, bcId: value, advertiserId: '', storeId: '' };
    setForm(next);
    setStoresByAdvertiser({});
    queryClient.removeQueries({ queryKey: ['gmv-max', 'products', wid || '', selectedAuthId || ''], exact: false });
    productsAnnouncementRef.current = false;
    saveScopeToLS({ wid, authId: selectedAuthId, bcId: next.bcId, advertiserId: next.advertiserId, storeId: next.storeId });
    syncUrl({ authId: selectedAuthId, bcId: next.bcId, advertiserId: next.advertiserId, storeId: next.storeId });
  }
  function handleAdvertiserChange(evt) {
    const value = evt.target.value || '';
    const next = { ...form, advertiserId: value, storeId: '' };
    setForm(next);
    queryClient.removeQueries({ queryKey: ['gmv-max', 'products', wid || '', selectedAuthId || ''], exact: false });
    productsAnnouncementRef.current = false;
    saveScopeToLS({ wid, authId: selectedAuthId, bcId: next.bcId, advertiserId: next.advertiserId, storeId: next.storeId });
    syncUrl({ authId: selectedAuthId, bcId: next.bcId, advertiserId: next.advertiserId, storeId: next.storeId });
  }
  function handleStoreChange(evt) {
    const value = evt.target.value || '';
    const next = { ...form, storeId: value };
    const prevStoreId = form.storeId;
    setForm(next);

    if (prevStoreId && prevStoreId !== value) {
      queryClient.removeQueries({ queryKey: ['gmv-max', 'products', wid || '', selectedAuthId || '', prevStoreId], exact: true });
    }

    if (value) {
      const cached = readProductsCache({ wid, authId: selectedAuthId, storeId: value });
      if (cached) {
        queryClient.setQueryData(['gmv-max', 'products', wid || '', selectedAuthId || '', value], cached);
        productsAnnouncementRef.current = false;
      } else {
        queryClient.removeQueries({ queryKey: ['gmv-max', 'products', wid || '', selectedAuthId || '', value], exact: true });
        productsAnnouncementRef.current = true;
      }
    } else {
      queryClient.removeQueries({ queryKey: ['gmv-max', 'products', wid || '', selectedAuthId || ''], exact: false });
      productsAnnouncementRef.current = false;
    }

    saveScopeToLS({ wid, authId: selectedAuthId, bcId: next.bcId, advertiserId: next.advertiserId, storeId: next.storeId });
    syncUrl({ authId: selectedAuthId, bcId: next.bcId, advertiserId: next.advertiserId, storeId: next.storeId });
  }
  function handleAutoSyncToggle(evt) {
    const checked = evt.target.checked;
    const next = { ...form, autoSyncProducts: checked };
    setForm(next);
    saveScopeToLS({ wid, authId: selectedAuthId, bcId: next.bcId, advertiserId: next.advertiserId, storeId: next.storeId });
  }

  /** 进入页面/切换 scope 时：若当前 store 命中缓存，直接恢复 */
  useEffect(() => {
    if (!wid || !selectedAuthId || !form.storeId) return;
    const cached = readProductsCache({ wid, authId: selectedAuthId, storeId: form.storeId });
    if (cached) {
      queryClient.setQueryData(['gmv-max', 'products', wid || '', selectedAuthId || '', form.storeId], cached);
      productsAnnouncementRef.current = false;
    }
  }, [form.storeId, queryClient, selectedAuthId, wid]);

  /** 刷新元数据 */
  function handleRefresh() {
    if (!wid || !selectedAuthId || refreshingOptions) return;
    refreshOptionsMutation.mutate();
  }

  /** 保存激活组合 */
  async function handleSave(evt) {
    evt.preventDefault();
    if (!wid || !selectedAuthId) return;
    if (!form.bcId || !form.advertiserId || !form.storeId) {
      setFeedback({ type: 'error', text: '请选择 Business Center、Advertiser 与 Store 后再保存。' });
      return;
    }
    setSaving(true);
    try {
      await saveBindingConfig(wid, provider, selectedAuthId, {
        bc_id: form.bcId,
        advertiser_id: form.advertiserId,
        store_id: form.storeId,
        auto_sync_products: !!form.autoSyncProducts,
      });
      setFeedback({ type: 'success', text: '已保存激活组合。' });
      setConfigVersion((prev) => prev + 1);
    } catch (error) {
      setFeedback({ type: 'error', text: extractErrorMessage(error, '保存失败，请检查选择是否一致') });
    } finally {
      setSaving(false);
    }
  }

  /** 手动触发同步 */
  async function handleProductSync() {
    if (!wid || !selectedAuthId) return;
    if (!form.bcId || !form.advertiserId || !form.storeId) {
      setFeedback({ type: 'error', text: '请选择完整的组合后再同步。' });
      return;
    }
    setTriggeringSync(true);
    setSyncRunStatus({ state: 'triggering', message: '正在触发 GMV Max 商品同步…' });
    try {
      const idempotencyKey = `sync-gmvmax-${Date.now()}`;
      const payload = {
        advertiserId: form.advertiserId,
        storeId: form.storeId,
        bcId: form.bcId,
        product_eligibility: 'gmv_max',
        mode: 'full',
        idempotency_key: idempotencyKey,
      };
      const response = await triggerProductSync(wid, provider, selectedAuthId, payload);
      const runId = response?.run_id;
      const responseIdem = response?.idempotency_key || idempotencyKey;

      if (runId) {
        setSyncRunStatus({ state: 'triggered', message: `同步任务已提交（运行 #${runId}），正在查询状态…`, runId });
      } else {
        setSyncRunStatus({ state: 'triggered', message: '同步任务已提交。' });
      }
      setFeedback({ type: 'success', text: responseIdem ? `已触发 GMV Max 商品同步（幂等键 ${responseIdem}）。` : '已触发 GMV Max 商品同步。' });

      if (runId) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        try {
          const run = await fetchSyncRun(wid, provider, selectedAuthId, runId);
          const described = describeRunStatus(run);
          setSyncRunStatus({ ...described, runId, run });
          if (described.state === 'failed') setFeedback({ type: 'error', text: described.detail || described.message });
          else if (described.state === 'succeeded') { setFeedback({ type: 'success', text: described.message }); setConfigVersion((prev) => prev + 1); }
          else setFeedback({ type: 'success', text: described.message });
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
  }

  /** 手动拉取商品（触发 React Query 重新拉取） */
  function handlePullProducts(refetchFn) {
    if (!wid || !selectedAuthId) return;
    if (!form.storeId) {
      setFeedback({ type: 'error', text: '请选择 Store 后再拉取商品。' });
      return;
    }
    productsAnnouncementRef.current = true;
    if (typeof refetchFn === 'function') refetchFn();
  }

  /** 禁用状态 */
  const disableRefresh = !selectedAuthId || refreshingOptions || optionsLoading;
  const disableSave =
    !selectedAuthId || saving || optionsLoading || advertisersLoading || loadingStores
    || !form.bcId || !form.advertiserId || !form.storeId;
  const disableManualSync =
    !selectedAuthId || triggeringSync || optionsLoading || advertisersLoading || loadingStores
    || !form.bcId || !form.advertiserId || !form.storeId;
  const productQueryFetching = productsQuery.isFetching;
  const disablePullProducts =
    !selectedAuthId || optionsLoading || advertisersLoading || !form.storeId || loadingStores || productQueryFetching;

  const lastManualSyncedAt = formatTimestamp(bindingConfig?.last_manual_synced_at);
  const lastManualSummaryText = formatSyncSummaryText(bindingConfig?.last_manual_sync_summary);
  const lastAutoSyncedAt = formatTimestamp(bindingConfig?.last_auto_synced_at);
  const lastAutoSummaryText = formatSyncSummaryText(bindingConfig?.last_auto_sync_summary);

  const hasBindings = bindings.length > 0;
  const productsData = productsQuery.data || null;
  const productItems = Array.isArray(productsData?.items) ? productsData.items : [];
  const productAvailableCount = Number.isFinite(Number(productsData?.availableCount))
    ? Number(productsData.availableCount)
    : productItems.length;
  const productFilteredOut = Number.isFinite(Number(productsData?.filteredOut))
    ? Number(productsData.filteredOut)
    : 0;
  const productTotalFromApi = Number.isFinite(Number(productsData?.totalFromApi))
    ? Number(productsData.totalFromApi)
    : (Number.isFinite(Number(productsData?.rawItemsCount))
      ? Number(productsData.rawItemsCount)
      : productItems.length);
  const productsLastUpdatedText = formatLocalTimeFromMillis(productsData?.receivedAt);
  const hasProductItems = productItems.length > 0;
  const productPreviewLimit = 24;
  const productVisibleItems = hasProductItems ? productItems.slice(0, productPreviewLimit) : [];
  const productsInitialLoading = productsQuery.isLoading && !initialProductsCache;
  const productsRefetching = productsQuery.isFetching && !!form.storeId;
  const productsSummaryText = !productsQueryEnabled
    ? '请选择 Store 查看 GMV Max 商品。'
    : [
      `可用 ${productAvailableCount} 条`,
      productFilteredOut > 0 ? `已过滤 ${productFilteredOut} 条不可用` : null,
      productTotalFromApi > productAvailableCount ? `API 返回 ${productTotalFromApi} 条` : null,
    ].filter(Boolean).join(' · ');

  /** 选项数据构建（保持你现状） */
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

  const bcOptions = useMemo(() => buildBusinessCenterOptions(optionsData, form.bcId), [optionsData, form.bcId]);
  const advertiserOptions = useMemo(() => buildAdvertiserOptions(optionsData, form.bcId, form.advertiserId), [optionsData, form.bcId, form.advertiserId]);
  const storeOptions = useMemo(() => buildStoreOptions(optionsData, form.advertiserId, form.storeId, form.bcId), [optionsData, form.advertiserId, form.storeId, form.bcId]);

  /** ---------------------------
   * 渲染
   * --------------------------- */
  return (
    <div className="gmv-max-page">
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

      <div className="card" style={{ display: 'grid', gap: '18px' }}>
        <FormField label="选择绑定 (Binding Alias)">
          <select className="form-input" value={selectedAuthId} onChange={handleBindingChange} disabled={bindingsLoading}>
            {!hasBindings && <option value="">暂无可用绑定</option>}
            {hasBindings && <option value="">请选择绑定</option>}
            {bindings.map((item) => {
              const value = String(item.auth_id);
              const alias = item.alias?.trim();
              const label = alias ? `${alias}（#${value}）` : `授权 #${value}`;
              return <option key={value} value={value}>{label}</option>;
            })}
          </select>
        </FormField>

        <div className="gmv-form-grid">
          <FormField label="Business Center">
            <select className="form-input" value={form.bcId} onChange={handleBcChange} disabled={!selectedAuthId || loadingConfig || optionsLoading}>
              <option value="">请选择 Business Center</option>
              {bcOptions.map((item, idx) => {
                const value = item?.bc_id ? String(item.bc_id) : '';
                const label = formatOptionLabel(resolveBusinessCenterName(item), value);
                return <option key={value || `missing-bc-${idx}`} value={value}>{label}</option>;
              })}
            </select>
            {(loadingConfig || optionsLoading) && <div className="small-muted">加载 Business Center...</div>}
          </FormField>

          <FormField label="Advertiser">
            <select className="form-input" value={form.advertiserId} onChange={handleAdvertiserChange} disabled={!selectedAuthId || optionsLoading || advertisersLoading}>
              <option value="">请选择 Advertiser</option>
              {advertiserOptions.map((item, idx) => {
                const value = item?.advertiser_id ? String(item.advertiser_id) : '';
                const label = formatOptionLabel(resolveAdvertiserName(item), value);
                return <option key={value || `missing-adv-${idx}`} value={value}>{label}</option>;
              })}
            </select>
            {(optionsLoading || advertisersLoading) && <div className="small-muted">加载 Advertiser...</div>}
          </FormField>

          <FormField label="Store">
            <select className="form-input" value={form.storeId} onChange={handleStoreChange} disabled={!selectedAuthId || !form.advertiserId || optionsLoading || loadingStores}>
              <option value="">请选择 Store</option>
              {storeOptions.map((item, idx) => {
                const value = item?.store_id ? String(item.store_id) : '';
                const baseLabel = formatOptionLabel(resolveStoreName(item), value);
                const normalizedOwner = form.bcId ? String(form.bcId) : '';
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
            {(optionsLoading || loadingStores) && <div className="small-muted">加载 Store...</div>}
          </FormField>
        </div>

        <FormField label="自动同步商品">
          <label className="checkbox-inline">
            <input type="checkbox" checked={form.autoSyncProducts} onChange={handleAutoSyncToggle} disabled={!selectedAuthId} />
            <span>开启后将按照后端计划自动同步 GMV Max 商品</span>
          </label>
          <div className="small-muted" style={{ marginTop: '6px' }}>
            最近一次自动同步：{lastAutoSyncedAt || '暂无记录'}{lastAutoSummaryText ? `（${lastAutoSummaryText}）` : ''}
          </div>
        </FormField>

        <div className="gmv-actions">
          <button className="btn ghost" type="button" onClick={handleRefresh} disabled={disableRefresh}>
            {refreshingOptions ? '刷新中…' : '刷新/同步'}
          </button>
          <button className="btn" type="button" onClick={handleSave} disabled={disableSave}>
            {saving ? '保存中…' : '保存激活组合'}
          </button>
          <button
            className="btn"
            type="button"
            onClick={() => handlePullProducts(productsQuery.refetch)}
            disabled={disablePullProducts}
          >
            {productsRefetching ? '刷新中…' : '拉取 GMV Max 商品'}
          </button>
          <button className="btn" type="button" onClick={handleProductSync} disabled={disableManualSync}>
            {triggeringSync ? '同步中…' : '同步 GMV Max 商品'}
          </button>
        </div>
        <div className="small-muted" style={{ marginTop: '4px' }}>
          最近一次手动同步：{lastManualSyncedAt || '暂无记录'}{lastManualSummaryText ? `（${lastManualSummaryText}）` : ''}
        </div>
        {syncRunStatus?.message && (
          <div className="small-muted" style={{ marginTop: '2px', color: (syncRunStatus.state === 'failed' || syncRunStatus.state === 'error') ? '#d1433f' : undefined }}>
            {syncRunStatus.message}{syncRunStatus.detail ? `（${syncRunStatus.detail}）` : ''}
          </div>
        )}
      </div>

      <div className="card gmv-products-card">
        <div className="gmv-products__header">
          <div>
            <h3 style={{ margin: 0 }}>GMV Max 商品</h3>
            <div className="small-muted">{productsSummaryText}</div>
            {productsLastUpdatedText && <div className="small-muted">最近更新：{productsLastUpdatedText}</div>}
          </div>
          <div className="gmv-products__actions">
            <button
              className="btn ghost"
              type="button"
              onClick={() => handlePullProducts(productsQuery.refetch)}
              disabled={disablePullProducts}
            >
              {productsRefetching ? '刷新中…' : '刷新商品列表'}
            </button>
          </div>
        </div>

        {!productsQueryEnabled ? (
          <div className="gmv-products__empty small-muted">请选择 Store 查看 GMV Max 商品。</div>
        ) : !hasProductItems ? (
          <div className="gmv-products__empty small-muted">
            {productsInitialLoading || productsRefetching
              ? '正在拉取 GMV Max 商品…'
              : '暂无可用商品，请稍后重试或执行同步。'}
          </div>
        ) : (
          <div className="gmv-products__grid">
            {productVisibleItems.map((item, idx) => {
              const pid = item?.product_id ? String(item.product_id) : '';
              const title = resolveProductTitle(item) || pid || `商品 ${idx + 1}`;
              const imageUrl = extractProductImage(item);
              const itemGroupId = resolveItemGroupId(item);
              const availabilityText = describeAvailabilityStatus(item);
              const occupied = isOccupiedByGmvMax(item);
              const updatedAt = formatTimestamp(
                item?.updated_time
                || item?.ext_updated_time
                || item?.last_updated_time
                || item?.last_update_time,
              );
              const priceText = item?.price_range
                || item?.price?.display
                || item?.price?.text
                || (item?.min_price && item?.max_price ? `${item.min_price} ~ ${item.max_price}` : null);
              return (
                <article
                  key={pid || itemGroupId || `product-${idx}`}
                  className={`gmv-product-card${occupied ? ' is-occupied' : ''}`}
                >
                  <div className="gmv-product-card__thumb">
                    {imageUrl ? (
                      <img src={imageUrl} alt={`${title} 主图`} loading="lazy" />
                    ) : (
                      <span className="gmv-product-card__thumb--placeholder">无主图</span>
                    )}
                  </div>
                  <div className="gmv-product-card__body">
                    <div className="gmv-product-card__header">
                      <div className="gmv-product-card__title">{title}</div>
                      <div className="gmv-product-card__badges">
                        <span
                          className={`gmv-product-card__badge ${occupied
                            ? 'gmv-product-card__badge--occupied'
                            : 'gmv-product-card__badge--free'}`}
                        >
                          {occupied ? 'GMV Max 占用' : '未占用'}
                        </span>
                        {availabilityText && (
                          <span className="gmv-product-card__badge gmv-product-card__badge--status">
                            {availabilityText}
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="gmv-product-card__meta">
                      <div>商品 ID：{pid || '-'}</div>
                      <div>Item Group：{itemGroupId || '-'}</div>
                      {priceText && <div>价格：{priceText}</div>}
                      {updatedAt && <div>更新时间：{updatedAt}</div>}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        ))}

        {hasProductItems && productItems.length > productPreviewLimit && (
          <div className="small-muted">已展示前 {productPreviewLimit} 条，可用总数 {productAvailableCount} 条。</div>
        )}
        {productFilteredOut > 0 && productsQueryEnabled && (
          <div className="small-muted">已自动过滤 {productFilteredOut} 条不可用商品。</div>
        )}
      </div>

      <SummaryPanel summary={metaSummary} />
    </div>
  );
}

