import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';

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

function extractErrorMessage(error, fallback = '操作失败') {
  if (!error) return fallback;
  const raw = error.message || (typeof error === 'string' ? error : '') || fallback;
  const cleaned = raw.startsWith('Error: ') ? raw.slice(7) : raw;
  try {
    const parsed = JSON.parse(cleaned);
    if (parsed?.error?.message) return parsed.error.message;
    if (parsed?.detail) return parsed.detail;
  } catch (_) {
    // ignore
  }
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
  if (!run) {
    return { state: 'unknown', message: '同步任务已提交，正在等待状态更新。' };
  }

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
      return {
        state: 'failed',
        message: `同步任务失败${runTag}。`,
        detail: detailParts.join('；') || null,
      };
    }
    case 'canceled':
    case 'cancelled':
      return { state: 'canceled', message: `同步任务已取消${runTag}。` };
    default:
      if (!status) {
        return { state: 'unknown', message: `同步任务状态未知${runTag}。` };
      }
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
        {SUMMARY_SECTIONS.map((section) => {
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

export default function GmvMaxManagementPage() {
  const { wid } = useParams();
  const provider = useMemo(() => normProvider(), []);

  const [bindings, setBindings] = useState([]);
  const [bindingsLoading, setBindingsLoading] = useState(false);
  const [selectedAuthId, setSelectedAuthId] = useState('');
  const [configVersion, setConfigVersion] = useState(0);

  const [form, setForm] = useState(DEFAULT_FORM);
  const formRef = useRef(DEFAULT_FORM);
  const [loadingConfig, setLoadingConfig] = useState(false);
  const [bindingConfig, setBindingConfig] = useState(null);

  const [businessCenters, setBusinessCenters] = useState([]);
  const [allAdvertisers, setAllAdvertisers] = useState([]);
  const [visibleAdvertisers, setVisibleAdvertisers] = useState([]);
  const [advertisersLoading, setAdvertisersLoading] = useState(false);
  const [storesByAdvertiser, setStoresByAdvertiser] = useState({});
  const [loadingStores, setLoadingStores] = useState(false);
  const [productsState, setProductsState] = useState({ items: [], total: 0, page: 1, pageSize: 10 });
  const [loadingProducts, setLoadingProducts] = useState(false);
  const [optionsEtag, setOptionsEtag] = useState(null);
  const [optionsLoading, setOptionsLoading] = useState(false);

  const [refreshingOptions, setRefreshingOptions] = useState(false);
  const [triggeringSync, setTriggeringSync] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [metaSummary, setMetaSummary] = useState(null);
  const [syncRunStatus, setSyncRunStatus] = useState(null);

  useEffect(() => {
    formRef.current = form;
  }, [form]);

  const fetchMetadataPayload = useCallback(async () => {
    if (!wid || !selectedAuthId) {
      return { bcs: [], advertisers: [] };
    }
    const [bcResponse, advertiserResponse] = await Promise.all([
      fetchBusinessCenters(wid, provider, selectedAuthId),
      fetchAdvertisers(wid, provider, selectedAuthId),
    ]);
    return {
      bcs: Array.isArray(bcResponse?.items) ? bcResponse.items : [],
      advertisers: Array.isArray(advertiserResponse?.items) ? advertiserResponse.items : [],
    };
  }, [wid, provider, selectedAuthId]);

  const applyMetadata = useCallback(
    ({ bcs, advertisers: advItems }) => {
      const safeBc = Array.isArray(bcs) ? bcs : [];
      const safeAdvertisers = Array.isArray(advItems) ? advItems : [];
      setBusinessCenters(safeBc);
      setAllAdvertisers(safeAdvertisers);
      const currentBcId = formRef.current?.bcId ? String(formRef.current.bcId) : '';
      setVisibleAdvertisers((prev) => (currentBcId ? prev : safeAdvertisers));
      setStoresByAdvertiser((prev) => {
        const retained = {};
        safeAdvertisers.forEach((adv) => {
          const advId = adv?.advertiser_id ? String(adv.advertiser_id) : '';
          if (advId && prev[advId]) {
            retained[advId] = prev[advId];
          }
        });
        return retained;
      });
      setForm((prev) => {
        let next = prev;
        const bcValid = !prev.bcId
          || safeBc.some((item) => String(item?.bc_id || '') === String(prev.bcId));
        if (!bcValid) {
          next = { ...prev, bcId: '', advertiserId: '', storeId: '' };
          return next;
        }
        const advValid = !prev.advertiserId
          || safeAdvertisers.some((item) => String(item?.advertiser_id || '') === String(prev.advertiserId));
        if (!advValid) {
          next = { ...prev, advertiserId: '', storeId: '' };
        }
        return next;
      });
    },
    [],
  );

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
      links: {
        bc_to_advertisers: bcLinks,
        advertiser_to_stores: advertiserStoreLinks,
      },
    };
  }, [businessCenters, visibleAdvertisers, allAdvertisers, storesByAdvertiser]);

  const bcOptions = useMemo(
    () => buildBusinessCenterOptions(optionsData, form.bcId),
    [optionsData, form.bcId],
  );

  const advertiserOptions = useMemo(
    () => buildAdvertiserOptions(optionsData, form.bcId, form.advertiserId),
    [optionsData, form.bcId, form.advertiserId],
  );

  const storeOptions = useMemo(
    () => buildStoreOptions(optionsData, form.advertiserId, form.storeId, form.bcId),
    [optionsData, form.advertiserId, form.storeId, form.bcId],
  );

  useEffect(() => {
    if (!wid) {
      setBindings([]);
      setSelectedAuthId('');
      return;
    }
    let ignore = false;
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
      .finally(() => {
        if (!ignore) setBindingsLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [wid]);

  useEffect(() => {
    if (!bindings?.length) {
      setSelectedAuthId('');
      setMetaSummary(null);
      setForm(DEFAULT_FORM);
      setBusinessCenters([]);
      setAllAdvertisers([]);
      setVisibleAdvertisers([]);
      setAdvertisersLoading(false);
      setStoresByAdvertiser({});
      setProductsState({ items: [], total: 0, page: 1, pageSize: 10 });
      setOptionsEtag(null);
      return;
    }
    setSelectedAuthId((prev) => {
      if (prev && bindings.some((item) => String(item.auth_id) === String(prev))) {
        return prev;
      }
      const first = bindings[0];
      return first ? String(first.auth_id) : '';
    });
  }, [bindings]);

  useEffect(() => {
    setMetaSummary(null);
    setForm(DEFAULT_FORM);
    setBindingConfig(null);
    setBusinessCenters([]);
    setAllAdvertisers([]);
    setVisibleAdvertisers([]);
    setAdvertisersLoading(false);
    setStoresByAdvertiser({});
    setProductsState({ items: [], total: 0, page: 1, pageSize: 10 });
    setOptionsEtag(null);
    setSyncRunStatus(null);
  }, [selectedAuthId]);

  useEffect(() => {
    if (!wid || !selectedAuthId) {
      setOptionsEtag(null);
      setOptionsLoading(false);
      return;
    }
    let ignore = false;
    const controller = new AbortController();
    let pending = 2;
    setOptionsLoading(true);

    const finish = () => {
      pending -= 1;
      if (!ignore && pending <= 0) {
        setOptionsLoading(false);
      }
    };

    fetchGmvOptions(wid, provider, selectedAuthId, { signal: controller.signal })
      .then(({ status, data, etag }) => {
        if (ignore) return;
        if (status === 200 && data) {
          const { refresh: refreshStatus, idempotency_key: refreshKey, summary } = data || {};
          setMetaSummary(summary || null);
          setOptionsEtag(etag || null);
          if (refreshStatus === 'timeout' && refreshKey) {
            setFeedback({ type: 'info', text: `已触发刷新，稍后再试（幂等键 ${refreshKey}）。` });
          }
        } else if (status === 304) {
          setOptionsEtag((prev) => etag || prev || null);
        }
      })
      .catch((error) => {
        if (!ignore) {
          setOptionsEtag(null);
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 GMV Max 选项') });
        }
      })
      .finally(() => {
        if (!ignore) finish();
      });

    fetchMetadataPayload()
      .then((metadata) => {
        if (ignore) return;
        applyMetadata(metadata);
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
      .finally(() => {
        if (!ignore) finish();
      });

    return () => {
      ignore = true;
      controller.abort();
    };
  }, [wid, provider, selectedAuthId, fetchMetadataPayload, applyMetadata]);

  useEffect(() => {
    if (!wid || !selectedAuthId) {
      setVisibleAdvertisers([]);
      setAdvertisersLoading(false);
      return;
    }
    let ignore = false;
    setAdvertisersLoading(true);
    const params = form.bcId ? { owner_bc_id: form.bcId } : {};
    fetchAdvertisers(wid, provider, selectedAuthId, params)
      .then((response) => {
        if (ignore) return;
        const items = Array.isArray(response?.items) ? response.items : [];
        setVisibleAdvertisers(items);
        setStoresByAdvertiser((prev) => {
          if (!form.bcId) {
            return prev;
          }
          const allowed = new Set(items.map((item) => String(item?.advertiser_id || '')));
          const next = {};
          Object.entries(prev || {}).forEach(([advId, stores]) => {
            if (allowed.has(String(advId))) {
              next[advId] = stores;
            }
          });
          return next;
        });
      })
      .catch((error) => {
        if (!ignore) {
          setVisibleAdvertisers([]);
          const text = extractErrorMessage(error, '无法加载 Advertiser 列表');
          setFeedback({ type: 'error', text });
        }
      })
      .finally(() => {
        if (!ignore) {
          setAdvertisersLoading(false);
        }
      });
    return () => {
      ignore = true;
    };
  }, [wid, provider, selectedAuthId, form.bcId]);

  useEffect(() => {
    if (!wid || !selectedAuthId) {
      setLoadingConfig(false);
      return;
    }
    let ignore = false;
    setLoadingConfig(true);
    async function load() {
      try {
        const config = await fetchBindingConfig(wid, provider, selectedAuthId);
        if (ignore) return;
        const resolvedConfig = config || null;
        setBindingConfig(resolvedConfig);
        setForm({
          bcId: resolvedConfig?.bc_id ?? '',
          advertiserId: resolvedConfig?.advertiser_id ?? '',
          storeId: resolvedConfig?.store_id ?? '',
          autoSyncProducts: Boolean(resolvedConfig?.auto_sync_products),
        });
      } catch (error) {
        if (!ignore) {
          setBindingConfig(null);
          setForm(DEFAULT_FORM);
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 GMV Max 配置') });
        }
      } finally {
        if (!ignore) {
          setLoadingConfig(false);
        }
      }
    }
    load();
    return () => {
      ignore = true;
    };
  }, [wid, provider, selectedAuthId, configVersion]);

  useEffect(() => {
    if (!wid || !selectedAuthId || !form.advertiserId) {
      setLoadingStores(false);
      return;
    }
    const existing = storesByAdvertiser[form.advertiserId];
    if (existing) {
      const hasSelected = existing.some((item) => String(item?.store_id || '') === String(form.storeId));
      if (!hasSelected && form.storeId) {
        setForm((prev) => (prev.advertiserId === form.advertiserId ? { ...prev, storeId: '' } : prev));
      }
      return;
    }
    let ignore = false;
    setLoadingStores(true);
    fetchStores(wid, provider, selectedAuthId, form.advertiserId, { owner_bc_id: form.bcId || undefined })
      .then((response) => {
        if (ignore) return;
        const storeItems = Array.isArray(response?.items) ? response.items : [];
        setStoresByAdvertiser((prev) => ({ ...prev, [form.advertiserId]: storeItems }));
        if (!storeItems.some((item) => String(item?.store_id || '') === String(form.storeId))) {
          setForm((prev) => (prev.advertiserId === form.advertiserId ? { ...prev, storeId: '' } : prev));
        }
      })
      .catch((error) => {
        if (!ignore) {
          setStoresByAdvertiser((prev) => ({ ...prev, [form.advertiserId]: [] }));
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 Store 列表') });
        }
      })
      .finally(() => {
        if (!ignore) {
          setLoadingStores(false);
        }
      });
    return () => {
      ignore = true;
    };
  }, [wid, provider, selectedAuthId, form.advertiserId, form.storeId, storesByAdvertiser]);

  function handleBindingChange(evt) {
    setSelectedAuthId(evt.target.value || '');
  }

  function handleBcChange(evt) {
    const value = evt.target.value || '';
    setForm((prev) => ({
      ...prev,
      bcId: value,
      advertiserId: '',
      storeId: '',
    }));
    setStoresByAdvertiser({});
    setProductsState({ items: [], total: 0, page: 1, pageSize: 10 });
  }

  function handleAdvertiserChange(evt) {
    const value = evt.target.value || '';
    setForm((prev) => ({
      ...prev,
      advertiserId: value,
      storeId: '',
    }));
    setProductsState({ items: [], total: 0, page: 1, pageSize: 10 });
  }

  function handleStoreChange(evt) {
    const value = evt.target.value || '';
    setForm((prev) => ({
      ...prev,
      storeId: value,
    }));
    setProductsState({ items: [], total: 0, page: 1, pageSize: 10 });
  }

  function handleAutoSyncToggle(evt) {
    const checked = evt.target.checked;
    setForm((prev) => ({
      ...prev,
      autoSyncProducts: checked,
    }));
  }

  async function handleRefresh() {
    if (!wid || !selectedAuthId) return;
    setRefreshingOptions(true);
    try {
      const { status, data, etag } = await fetchGmvOptions(wid, provider, selectedAuthId, {
        refresh: true,
        etag: optionsEtag,
      });
      if (status === 304) {
        setFeedback({ type: 'info', text: '元数据未发生变化。' });
        if (etag) {
          setOptionsEtag(etag);
        }
      } else if (data) {
        const { refresh: refreshStatus, idempotency_key: refreshKey, summary } = data || {};
        setMetaSummary(summary || null);
        if (etag) setOptionsEtag(etag);
        if (refreshStatus === 'timeout') {
          const timeoutText = refreshKey
            ? `已触发刷新，稍后再试（幂等键 ${refreshKey}）。`
            : '已触发刷新，稍后再试。';
          setFeedback({ type: 'info', text: timeoutText });
        } else {
          const baseText = '已刷新最新可选项。';
          const successText = refreshKey ? `${baseText}（幂等键 ${refreshKey}）` : baseText;
          setFeedback({ type: 'success', text: successText });
        }
      }
      try {
        const metadata = await fetchMetadataPayload();
        applyMetadata(metadata);
      } catch (metaError) {
        setFeedback({ type: 'error', text: extractErrorMessage(metaError, '刷新成功，但更新 TikTok 元数据失败') });
      }
    } catch (error) {
      setFeedback({ type: 'error', text: extractErrorMessage(error, '刷新失败，请稍后重试') });
    } finally {
      setRefreshingOptions(false);
    }
  }

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
        eligibility: 'gmv_max',
        mode: 'full',
        idempotency_key: idempotencyKey,
      };

      const response = await triggerProductSync(wid, provider, selectedAuthId, payload);
      const runId = response?.run_id;
      const responseIdem = response?.idempotency_key || idempotencyKey;

      if (runId) {
        setSyncRunStatus({
          state: 'triggered',
          message: `同步任务已提交（运行 #${runId}），正在查询状态…`,
          runId,
        });
      } else {
        setSyncRunStatus({ state: 'triggered', message: '同步任务已提交。' });
      }

      setFeedback({
        type: 'success',
        text: responseIdem ? `已触发 GMV Max 商品同步（幂等键 ${responseIdem}）。` : '已触发 GMV Max 商品同步。',
      });

      if (runId) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        try {
          const run = await fetchSyncRun(wid, provider, selectedAuthId, runId);
          const described = describeRunStatus(run);
          setSyncRunStatus({ ...described, runId, run });
          if (described.state === 'failed') {
            setFeedback({ type: 'error', text: described.detail || described.message });
          } else if (described.state === 'succeeded') {
            setFeedback({ type: 'success', text: described.message });
            setConfigVersion((prev) => prev + 1);
          } else {
            setFeedback({ type: 'success', text: described.message });
          }
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
      if (error?.status === 409) {
        text = '同步进行中，请稍后再试。';
      } else if (error?.status === 429) {
        text = '同步过于频繁，请稍后再试。';
      } else {
        text = extractErrorMessage(error, '同步失败，请稍后再试。');
      }
      setFeedback({ type: 'error', text });
      setSyncRunStatus({ state: 'error', message: text });
    } finally {
      setTriggeringSync(false);
    }
  }

  async function handlePullProducts() {
    if (!wid || !selectedAuthId) return;
    if (!form.storeId) {
      setFeedback({ type: 'error', text: '请选择 Store 后再拉取商品。' });
      return;
    }
    setLoadingProducts(true);
    try {
      const data = await fetchProducts(wid, provider, selectedAuthId, form.storeId);
      const items = Array.isArray(data?.items) ? data.items : [];
      const total = Number.isFinite(Number(data?.total)) ? Number(data.total) : items.length;
      const page = Number.isFinite(Number(data?.page)) ? Number(data.page) : 1;
      const pageSizeRaw = data?.page_size ?? data?.pageSize;
      const pageSize = Number.isFinite(Number(pageSizeRaw)) ? Number(pageSizeRaw) : items.length || 10;
      setProductsState({ items, total, page, pageSize });
      if (total === 0 || items.length === 0) {
        setFeedback({ type: 'info', text: '未找到 GMV Max 商品。' });
      } else {
        const previewCount = Math.min(items.length, 10);
        const message = `已拉取 ${total} 条 GMV Max 商品（展示前 ${previewCount} 条）。`;
        setFeedback({ type: 'success', text: message });
      }
    } catch (error) {
      setProductsState({ items: [], total: 0, page: 1, pageSize: 10 });
      setFeedback({ type: 'error', text: extractErrorMessage(error, '拉取 GMV Max 商品失败，请稍后重试') });
    } finally {
      setLoadingProducts(false);
    }
  }

  const disableRefresh = !selectedAuthId || refreshingOptions || optionsLoading;
  const disableSave =
    !selectedAuthId
    || saving
    || optionsLoading
    || advertisersLoading
    || loadingStores
    || !form.bcId
    || !form.advertiserId
    || !form.storeId;
  const disableManualSync =
    !selectedAuthId
    || triggeringSync
    || optionsLoading
    || advertisersLoading
    || loadingStores
    || !form.bcId
    || !form.advertiserId
    || !form.storeId;
  const disablePullProducts =
    !selectedAuthId
    || optionsLoading
    || advertisersLoading
    || !form.storeId
    || loadingProducts
    || loadingStores;

  const lastManualSyncedAt = formatTimestamp(bindingConfig?.last_manual_synced_at);
  const lastManualSummaryText = formatSyncSummaryText(bindingConfig?.last_manual_sync_summary);
  const lastAutoSyncedAt = formatTimestamp(bindingConfig?.last_auto_synced_at);
  const lastAutoSummaryText = formatSyncSummaryText(bindingConfig?.last_auto_sync_summary);

  const hasBindings = bindings.length > 0;

  return (
    <div className="gmv-max-page">
      <div className="page-header">
        <div>
          <h1>GMV Max 管理</h1>
          <p className="small-muted">绑定后可配置 Business Center / Advertiser / Store，并触发元数据刷新。</p>
        </div>
      </div>

      {feedback?.text && (
        <div
          className={`alert ${feedback.type === 'error' ? 'alert--error' : feedback.type === 'success' ? 'alert--success' : ''}`}
        >
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
              return (
                <option key={value} value={value}>
                  {label}
                </option>
              );
            })}
          </select>
        </FormField>

        <div className="gmv-form-grid">
          <FormField label="Business Center">
            <select
              className="form-input"
              value={form.bcId}
              onChange={handleBcChange}
              disabled={!selectedAuthId || loadingConfig || optionsLoading}
            >
              <option value="">请选择 Business Center</option>
              {bcOptions.map((item, idx) => {
                const value = item?.bc_id ? String(item.bc_id) : '';
                const label = formatOptionLabel(resolveBusinessCenterName(item), value);
                return (
                  <option key={value || `missing-bc-${idx}`} value={value}>
                    {label}
                  </option>
                );
              })}
            </select>
            {(loadingConfig || optionsLoading) && <div className="small-muted">加载 Business Center...</div>}
          </FormField>

          <FormField label="Advertiser">
            <select
              className="form-input"
              value={form.advertiserId}
              onChange={handleAdvertiserChange}
              disabled={!selectedAuthId || optionsLoading || advertisersLoading}
            >
              <option value="">请选择 Advertiser</option>
              {advertiserOptions.map((item, idx) => {
                const value = item?.advertiser_id ? String(item.advertiser_id) : '';
                const label = formatOptionLabel(resolveAdvertiserName(item), value);
                return (
                  <option key={value || `missing-adv-${idx}`} value={value}>
                    {label}
                  </option>
                );
              })}
            </select>
            {(optionsLoading || advertisersLoading) && <div className="small-muted">加载 Advertiser...</div>}
          </FormField>

          <FormField label="Store">
            <select
              className="form-input"
              value={form.storeId}
              onChange={handleStoreChange}
              disabled={!selectedAuthId || !form.advertiserId || optionsLoading || loadingStores}
            >
              <option value="">请选择 Store</option>
              {storeOptions.map((item, idx) => {
                const value = item?.store_id ? String(item.store_id) : '';
                const baseLabel = formatOptionLabel(resolveStoreName(item), value);
                const normalizedOwner = form.bcId ? String(form.bcId) : '';
                let label = baseLabel;
                if (normalizedOwner) {
                  const authorizedCandidates = [
                    item?.store_authorized_bc_id,
                    item?.bc_id,
                    item?.bc_id_hint,
                  ]
                    .map((candidate) => {
                      if (candidate === undefined || candidate === null) return '';
                      return String(candidate);
                    })
                    .filter(Boolean);
                  const isAuthorized = authorizedCandidates.some((candidate) => candidate === normalizedOwner);
                  if (!isAuthorized) {
                    label = `${baseLabel}（未确认授权）`;
                  }
                }
                return (
                  <option key={value || `missing-store-${idx}`} value={value}>
                    {label}
                  </option>
                );
              })}
            </select>
            {(optionsLoading || loadingStores) && <div className="small-muted">加载 Store...</div>}
          </FormField>
        </div>

        <FormField label="自动同步商品">
          <label className="checkbox-inline">
            <input
              type="checkbox"
              checked={form.autoSyncProducts}
              onChange={handleAutoSyncToggle}
              disabled={!selectedAuthId}
            />
            <span>开启后将按照后端计划自动同步 GMV Max 商品</span>
          </label>
          <div className="small-muted" style={{ marginTop: '6px' }}>
            最近一次自动同步：{lastAutoSyncedAt || '暂无记录'}
            {lastAutoSummaryText ? `（${lastAutoSummaryText}）` : ''}
          </div>
        </FormField>

        <div className="gmv-actions">
          <button
            className="btn ghost"
            type="button"
            onClick={handleRefresh}
            disabled={disableRefresh}
          >
            {refreshingOptions ? '刷新中…' : '刷新/同步'}
          </button>
          <button className="btn" type="button" onClick={handleSave} disabled={disableSave}>
            {saving ? '保存中…' : '保存激活组合'}
          </button>
          <button className="btn" type="button" onClick={handlePullProducts} disabled={disablePullProducts}>
            {loadingProducts ? '拉取中…' : '拉取 GMV Max 商品'}
          </button>
          <button className="btn" type="button" onClick={handleProductSync} disabled={disableManualSync}>
            {triggeringSync ? '同步中…' : '同步 GMV Max 商品'}
          </button>
        </div>
        <div className="small-muted" style={{ marginTop: '4px' }}>
          最近一次手动同步：{lastManualSyncedAt || '暂无记录'}
          {lastManualSummaryText ? `（${lastManualSummaryText}）` : ''}
        </div>
        {syncRunStatus?.message && (
          <div
            className="small-muted"
            style={{ marginTop: '2px', color: syncRunStatus.state === 'failed' || syncRunStatus.state === 'error' ? '#d1433f' : undefined }}
          >
            {syncRunStatus.message}
            {syncRunStatus.detail ? `（${syncRunStatus.detail}）` : ''}
          </div>
        )}
      </div>

      <div className="card" style={{ display: 'grid', gap: '12px' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: '12px',
          }}
        >
          <h3 style={{ margin: 0 }}>GMV Max 商品</h3>
          <div className="small-muted">当前共 {productsState.total || 0} 条</div>
        </div>
        {productsState.items.length === 0 ? (
          <div className="small-muted">
            {loadingProducts ? '正在拉取 GMV Max 商品…' : '请选择 Store 并点击“拉取 GMV Max 商品”查看列表。'}
          </div>
        ) : (
          <div className="product-preview" style={{ display: 'grid', gap: '6px' }}>
            <div className="small-muted">
              展示前 {Math.min(productsState.items.length, 10)} 条（共 {productsState.total} 条）
            </div>
            <ol className="product-preview__list" style={{ paddingLeft: '18px', margin: 0, display: 'grid', gap: '8px' }}>
              {productsState.items.slice(0, 10).map((item, idx) => {
                const pid = item?.product_id ? String(item.product_id) : '';
                const title = resolveProductTitle(item) || pid || `商品 ${idx + 1}`;
                const status = item?.status ? String(item.status) : '';
                const updatedAt = formatTimestamp(item?.updated_time || item?.ext_updated_time);
                const skuText = Number.isFinite(Number(item?.sku_count)) ? `SKU：${Number(item.sku_count)}` : null;
                return (
                  <li key={pid || `product-${idx}`} className="product-preview__item" style={{ listStyle: 'decimal inside' }}>
                    <div className="product-preview__title" style={{ fontWeight: 500 }}>{title}</div>
                    <div className="small-muted" style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                      <span>商品 ID：{pid || '-'}</span>
                      {status && <span>状态：{status}</span>}
                      {item?.price_range && <span>价格区间：{item.price_range}</span>}
                      {skuText && <span>{skuText}</span>}
                      {updatedAt && <span>更新时间：{updatedAt}</span>}
                    </div>
                  </li>
                );
              })}
            </ol>
          </div>
        )}
      </div>

      <SummaryPanel summary={metaSummary} />
    </div>
  );
}
