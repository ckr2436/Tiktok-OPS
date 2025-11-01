import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';

import FormField from '../../../../components/ui/FormField.jsx';
import {
  fetchAdvertisers,
  fetchBindingConfig,
  fetchBusinessCenters,
  fetchSyncRun,
  fetchStores,
  listBindings,
  normProvider,
  saveBindingConfig,
  triggerMetaSync,
  triggerProductSync,
} from '../service.js';

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
  const [optionsVersion, setOptionsVersion] = useState(0);
  const [configVersion, setConfigVersion] = useState(0);

  const [form, setForm] = useState(DEFAULT_FORM);
  const [loadingConfig, setLoadingConfig] = useState(false);
  const [bindingConfig, setBindingConfig] = useState(null);

  const [bcOptions, setBcOptions] = useState([]);
  const [advertiserOptions, setAdvertiserOptions] = useState([]);
  const [storeOptions, setStoreOptions] = useState([]);

  const [loadingAdvertisers, setLoadingAdvertisers] = useState(false);
  const [loadingStores, setLoadingStores] = useState(false);

  const [refreshingOptions, setRefreshingOptions] = useState(false);
  const [triggeringSync, setTriggeringSync] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [metaSummary, setMetaSummary] = useState(null);
  const [syncRunStatus, setSyncRunStatus] = useState(null);

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
      setBcOptions([]);
      setAdvertiserOptions([]);
      setStoreOptions([]);
      setOptionsVersion(0);
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
    setBcOptions([]);
    setAdvertiserOptions([]);
    setStoreOptions([]);
    setOptionsVersion(0);
    setSyncRunStatus(null);
  }, [selectedAuthId]);

  useEffect(() => {
    if (!wid || !selectedAuthId) {
      setLoadingConfig(false);
      return;
    }
    let ignore = false;
    setLoadingConfig(true);
    async function load() {
      try {
        const [config, centers] = await Promise.all([
          fetchBindingConfig(wid, provider, selectedAuthId),
          fetchBusinessCenters(wid, provider, selectedAuthId),
        ]);
        if (ignore) return;
        const resolvedConfig = config || null;
        setBindingConfig(resolvedConfig);
        setBcOptions(Array.isArray(centers) ? centers : []);
        setForm({
          bcId: resolvedConfig?.bc_id ?? '',
          advertiserId: resolvedConfig?.advertiser_id ?? '',
          storeId: resolvedConfig?.store_id ?? '',
          autoSyncProducts: Boolean(resolvedConfig?.auto_sync_products),
        });
      } catch (error) {
        if (!ignore) {
          setBindingConfig(null);
          setBcOptions([]);
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
  }, [wid, provider, selectedAuthId, optionsVersion, configVersion]);

  useEffect(() => {
    if (!wid || !selectedAuthId || !form.bcId) {
      setAdvertiserOptions([]);
      setLoadingAdvertisers(false);
      return;
    }
    let ignore = false;
    setLoadingAdvertisers(true);
    fetchAdvertisers(wid, provider, selectedAuthId, { bc_id: form.bcId })
      .then((items) => {
        if (!ignore) setAdvertiserOptions(Array.isArray(items) ? items : []);
      })
      .catch((error) => {
        if (!ignore) {
          setAdvertiserOptions([]);
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 Advertiser 列表') });
        }
      })
      .finally(() => {
        if (!ignore) setLoadingAdvertisers(false);
      });
    return () => {
      ignore = true;
    };
  }, [wid, provider, selectedAuthId, form.bcId, optionsVersion]);

  useEffect(() => {
    if (!wid || !selectedAuthId || !form.advertiserId) {
      setStoreOptions([]);
      setLoadingStores(false);
      return;
    }
    let ignore = false;
    setLoadingStores(true);
    fetchStores(wid, provider, selectedAuthId, { advertiser_id: form.advertiserId })
      .then((items) => {
        if (!ignore) setStoreOptions(Array.isArray(items) ? items : []);
      })
      .catch((error) => {
        if (!ignore) {
          setStoreOptions([]);
          setFeedback({ type: 'error', text: extractErrorMessage(error, '无法加载 Store 列表') });
        }
      })
      .finally(() => {
        if (!ignore) setLoadingStores(false);
      });
    return () => {
      ignore = true;
    };
  }, [wid, provider, selectedAuthId, form.advertiserId, optionsVersion]);

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
    setAdvertiserOptions([]);
    setStoreOptions([]);
  }

  function handleAdvertiserChange(evt) {
    const value = evt.target.value || '';
    setForm((prev) => ({
      ...prev,
      advertiserId: value,
      storeId: '',
    }));
    setStoreOptions([]);
  }

  function handleStoreChange(evt) {
    const value = evt.target.value || '';
    setForm((prev) => ({
      ...prev,
      storeId: value,
    }));
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
      const result = await triggerMetaSync(wid, provider, selectedAuthId);
      setMetaSummary(result?.summary || null);
      setFeedback({ type: 'success', text: '已刷新可选项，请查看摘要结果。' });
      setOptionsVersion((prev) => prev + 1);
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
      const payload = {
        scope: 'products',
        mode: 'full',
        product_eligibility: 'gmv_max',
        advertiser_id: form.advertiserId,
        store_id: form.storeId,
        idempotency_key: `sync-gmvmax-${Date.now()}`,
      };

      const response = await triggerProductSync(wid, provider, selectedAuthId, payload);
      const runId = response?.run_id;

      if (runId) {
        setSyncRunStatus({
          state: 'triggered',
          message: `同步任务已提交（运行 #${runId}），正在查询状态…`,
          runId,
        });
      } else {
        setSyncRunStatus({ state: 'triggered', message: '同步任务已提交。' });
      }

      setFeedback({ type: 'success', text: '已触发 GMV Max 商品同步。' });

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

  const disableRefresh = !selectedAuthId || refreshingOptions;
  const disableSave = !selectedAuthId || saving || !form.bcId || !form.advertiserId || !form.storeId;
  const disableManualSync =
    !selectedAuthId || triggeringSync || !form.bcId || !form.advertiserId || !form.storeId;

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
              disabled={!selectedAuthId || loadingConfig}
            >
              <option value="">请选择 Business Center</option>
              {bcOptions.map((item, idx) => {
                const value = item?.bc_id ? String(item.bc_id) : '';
                const name = item?.name || item?.raw?.name;
                const label = value ? `${value}${name ? ` · ${name}` : ''}` : '(缺少 bc_id)';
                return (
                  <option key={value || `missing-bc-${idx}`} value={value}>
                    {label}
                  </option>
                );
              })}
            </select>
            {loadingConfig && <div className="small-muted">加载 Business Center...</div>}
          </FormField>

          <FormField label="Advertiser">
            <select
              className="form-input"
              value={form.advertiserId}
              onChange={handleAdvertiserChange}
              disabled={!selectedAuthId || !form.bcId || loadingAdvertisers}
            >
              <option value="">请选择 Advertiser</option>
              {advertiserOptions.map((item, idx) => {
                const value = item?.advertiser_id ? String(item.advertiser_id) : '';
                const labelBase = item?.display_name || item?.name;
                const label = value ? `${value}${labelBase ? ` · ${labelBase}` : ''}` : '(缺少 advertiser_id)';
                return (
                  <option key={value || `missing-adv-${idx}`} value={value}>
                    {label}
                  </option>
                );
              })}
            </select>
            {loadingAdvertisers && <div className="small-muted">加载 Advertiser...</div>}
          </FormField>

          <FormField label="Store">
            <select
              className="form-input"
              value={form.storeId}
              onChange={handleStoreChange}
              disabled={!selectedAuthId || !form.advertiserId || loadingStores}
            >
              <option value="">请选择 Store</option>
              {storeOptions.map((item, idx) => {
                const value = item?.store_id ? String(item.store_id) : '';
                const name = item?.name;
                const label = value ? `${value}${name ? ` · ${name}` : ''}` : '(缺少 store_id)';
                return (
                  <option key={value || `missing-store-${idx}`} value={value}>
                    {label}
                  </option>
                );
              })}
            </select>
            {loadingStores && <div className="small-muted">加载 Store...</div>}
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
            {refreshingOptions ? '刷新中…' : '刷新可选项'}
          </button>
          <button className="btn" type="button" onClick={handleSave} disabled={disableSave}>
            {saving ? '保存中…' : '保存激活组合'}
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

      <SummaryPanel summary={metaSummary} />
    </div>
  );
}
