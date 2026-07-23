import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams } from 'react-router-dom';

import Loading from '@/components/ui/Loading.jsx';
import { useSessionQuery } from '@/features/platform/auth/hooks.js';
import {
  disableFlashSalePolicy,
  getCommerceContext,
  getCommerceOverview,
  getCommerceSyncStatus,
  getFlashSalePolicies,
  getProductCostHistory,
  saveFlashSalePolicy,
  saveProductCost,
  syncCommerceData,
} from './api.js';
import {
  advertisersForShop,
  errorMessage,
  formatDateTime,
  formatMoney,
  formatPercent,
  rangeForPreset,
  shortText,
} from './commerceUtils.js';
import './commerce.css';

function shopLocalTime(value) {
  const raw = String(value?.shop_local || '');
  if (!raw) return '等待首次检查';
  return raw.replace('T', ' ').slice(0, 16);
}

function flashSaleStatus(policy) {
  if (!policy) return { text: '未设置', tone: 'muted' };
  if (!policy.enabled) return { text: '已暂停', tone: 'muted' };
  if (policy.status === 'error') return { text: '需要处理', tone: 'error' };
  if (policy.applied_revision < policy.policy_revision) {
    return { text: '正在应用', tone: 'pending' };
  }
  return { text: '自动续期中', tone: 'success' };
}

function FlashSaleDialog({
  product,
  policy,
  shopId,
  workspaceId,
  onClose,
  onSaved,
}) {
  const [price, setPrice] = useState(
    policy?.activity_price_amount ? String(Number(policy.activity_price_amount)) : '',
  );
  const saveMutation = useMutation({
    mutationFn: () => saveFlashSalePolicy(workspaceId, product.product_id, {
      shop_id: Number(shopId),
      activity_price_amount: Number(price),
    }),
    onSuccess: onSaved,
  });
  const disableMutation = useMutation({
    mutationFn: () => disableFlashSalePolicy(workspaceId, product.product_id, Number(shopId)),
    onSuccess: onSaved,
  });
  const invalid = !Number.isFinite(Number(price)) || Number(price) <= 0;
  const status = flashSaleStatus(policy);

  return (
    <div
      className="modal-backdrop commerce-modal-backdrop"
      role="presentation"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <section
        className="modal commerce-flash-sale-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="flash-sale-dialog-title"
      >
        <header className="modal__header">
          <div>
            <div className="modal__title" id="flash-sale-dialog-title">闪购自动续期</div>
            <small title={product.title}>{shortText(product.title, 62)}</small>
          </div>
          <button type="button" className="modal__close" onClick={onClose} aria-label="关闭">×</button>
        </header>
        <div className="modal__body">
          {saveMutation.error || disableMutation.error ? (
            <div className="alert alert--error">
              {errorMessage(saveMutation.error || disableMutation.error)}
            </div>
          ) : null}
          <div className="commerce-flash-sale-summary">
            <div>
              <span>状态</span>
              <strong className={`commerce-flash-state commerce-flash-state--${status.tone}`}>
                {status.text}
              </strong>
            </div>
            <div>
              <span>当前覆盖至</span>
              <strong>{shopLocalTime(policy?.current_end_at)}</strong>
            </div>
            <div>
              <span>预计下次续期检查</span>
              <strong>{shopLocalTime(policy?.next_renewal_at)}</strong>
            </div>
          </div>
          <label className="commerce-flash-sale-price">
            <span>活动价</span>
            <div className="commerce-input-addon">
              <i>{product.currency || 'USD'}</i>
              <input
                type="number"
                min="0.01"
                step="0.01"
                value={price}
                autoFocus
                onChange={(event) => setPrice(event.target.value)}
                placeholder="输入活动价"
              />
            </div>
            <small>
              系统每 15 分钟检查一次，单次活动约 72 小时，并始终保持未来至少 2 天有效覆盖。
              时间按商店 UTC-8 换算。
            </small>
          </label>
          {policy?.last_error_message ? (
            <div className="alert alert--error">{policy.last_error_message}</div>
          ) : null}
        </div>
        <footer className="commerce-dialog-actions">
          {policy?.enabled ? (
            <button
              type="button"
              className="btn ghost commerce-danger-button"
              onClick={() => disableMutation.mutate()}
              disabled={disableMutation.isPending || saveMutation.isPending}
            >
              {disableMutation.isPending ? '正在暂停…' : '暂停自动续期'}
            </button>
          ) : null}
          <button type="button" className="btn ghost" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn"
            onClick={() => saveMutation.mutate()}
            disabled={invalid || saveMutation.isPending || disableMutation.isPending}
          >
            {saveMutation.isPending ? '正在保存…' : policy ? '保存活动价' : '开启自动续期'}
          </button>
        </footer>
      </section>
    </div>
  );
}

const FIXED_FIELDS = [
  ['unit_cost', '商品成本'],
  ['packaging_cost', '包装成本'],
  ['fulfillment_cost', '履约成本'],
  ['seller_shipping_cost', '卖家承担运费'],
  ['other_variable_cost', '其他单件成本'],
];

const RATE_FIELDS = [
  ['platform_fee_rate', '平台费率'],
  ['payment_fee_rate', '支付费率'],
  ['affiliate_commission_rate', '达人佣金率'],
  ['expected_refund_rate', '预计退款率'],
  ['target_margin_rate', '目标贡献率'],
];

const EMPTY_FORM = {
  currency: 'USD',
  unit_cost: '',
  packaging_cost: '',
  fulfillment_cost: '',
  seller_shipping_cost: '',
  other_variable_cost: '',
  platform_fee_rate: '',
  payment_fee_rate: '',
  affiliate_commission_rate: '',
  expected_refund_rate: '',
  target_margin_rate: '',
  effective_from: '',
  notes: '',
};

function formFromProduct(product) {
  const cost = product?.current_cost;
  if (!cost) {
    return {
      ...EMPTY_FORM,
      currency: product?.currency || 'USD',
    };
  }
  const result = {
    ...EMPTY_FORM,
    currency: cost.currency || product?.currency || 'USD',
    notes: cost.notes || '',
  };
  FIXED_FIELDS.forEach(([key]) => {
    result[key] = String(Number(cost[key] || 0));
  });
  RATE_FIELDS.forEach(([key]) => {
    result[key] = String(Number(cost[key] || 0) * 100);
  });
  return result;
}

function CostDialog({ product, shopId, workspaceId, onClose, onSaved }) {
  const [form, setForm] = useState(() => formFromProduct(product));
  const historyQuery = useQuery({
    queryKey: ['commerce', 'product-costs', workspaceId, shopId, product.product_id],
    queryFn: ({ signal }) => getProductCostHistory(
      workspaceId,
      product.product_id,
      { shop_id: shopId },
      { signal },
    ),
    enabled: Boolean(workspaceId && shopId && product.product_id),
  });
  const saveMutation = useMutation({
    mutationFn: () => {
      const effective = form.effective_from
        ? form.effective_from
        : null;
      return saveProductCost(workspaceId, product.product_id, {
        shop_id: Number(shopId),
        sku_id: '',
        effective_from: effective,
        currency: String(form.currency || 'USD').toUpperCase(),
        ...Object.fromEntries(
          FIXED_FIELDS.map(([key]) => [key, Number(form[key] || 0)]),
        ),
        ...Object.fromEntries(
          RATE_FIELDS.map(([key]) => [key, Number(form[key] || 0) / 100]),
        ),
        notes: form.notes.trim() || null,
      });
    },
    onSuccess: onSaved,
  });
  const combinedVariableRate = RATE_FIELDS
    .filter(([key]) => key !== 'target_margin_rate')
    .reduce((total, [key]) => total + Number(form[key] || 0), 0);
  const invalid = combinedVariableRate >= 100;
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const history = Array.isArray(historyQuery.data?.items) ? historyQuery.data.items : [];

  return (
    <div className="modal-backdrop commerce-modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="modal commerce-cost-dialog" role="dialog" aria-modal="true" aria-labelledby="cost-dialog-title">
        <header className="modal__header">
          <div>
            <div className="modal__title" id="cost-dialog-title">配置商品成本</div>
            <small title={product.title}>{shortText(product.title, 62)}</small>
          </div>
          <button type="button" className="modal__close" onClick={onClose} aria-label="关闭">×</button>
        </header>
        <div className="modal__body">
          {saveMutation.error ? <div className="alert alert--error">{errorMessage(saveMutation.error)}</div> : null}
          <div className="commerce-cost-grid">
            {FIXED_FIELDS.map(([key, label]) => (
              <label key={key}>
                <span>{label}</span>
                <div className="commerce-input-addon">
                  <i>{form.currency || 'USD'}</i>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={form[key]}
                    onChange={(event) => update(key, event.target.value)}
                  />
                </div>
              </label>
            ))}
            {RATE_FIELDS.map(([key, label]) => (
              <label key={key}>
                <span>{label}</span>
                <div className="commerce-input-addon commerce-input-addon--suffix">
                  <input
                    type="number"
                    min="0"
                    max="100"
                    step="0.1"
                    value={form[key]}
                    onChange={(event) => update(key, event.target.value)}
                  />
                  <i>%</i>
                </div>
              </label>
            ))}
            <label>
              <span>币种</span>
              <input
                type="text"
                maxLength="16"
                value={form.currency}
                onChange={(event) => update('currency', event.target.value.toUpperCase())}
              />
            </label>
            <label>
              <span>生效时间</span>
              <input
                type="datetime-local"
                value={form.effective_from}
                onChange={(event) => update('effective_from', event.target.value)}
              />
              <small>按商店固定时区 UTC-8 解释；留空表示立即生效。</small>
            </label>
          </div>
          <label className="commerce-notes">
            <span>备注</span>
            <textarea
              rows="3"
              maxLength="2000"
              value={form.notes}
              onChange={(event) => update('notes', event.target.value)}
              placeholder="例如：供应商报价、运费方案或佣金政策版本"
            />
          </label>
          {invalid ? <div className="alert alert--error">平台费、支付费、佣金和预计退款率之和必须低于 100%。</div> : null}

          <div className="commerce-history">
            <h3>成本版本</h3>
            {historyQuery.isLoading ? <Loading text="正在读取成本历史…" /> : history.length === 0 ? (
              <p>尚未保存成本版本。</p>
            ) : (
              <div className="commerce-table-wrap">
                <table className="commerce-table commerce-table--compact">
                  <thead><tr><th>生效时间</th><th>商品成本</th><th>单件固定成本</th><th>综合费率</th><th>备注</th></tr></thead>
                  <tbody>
                    {history.map((item) => {
                      const fixed = FIXED_FIELDS.reduce((total, [key]) => total + Number(item[key] || 0), 0);
                      const rate = RATE_FIELDS
                        .filter(([key]) => key !== 'target_margin_rate')
                        .reduce((total, [key]) => total + Number(item[key] || 0), 0);
                      return (
                        <tr key={item.id}>
                          <td>{formatDateTime(item.effective_from)}</td>
                          <td>{formatMoney(item.unit_cost, item.currency)}</td>
                          <td>{formatMoney(fixed, item.currency)}</td>
                          <td>{formatPercent(rate)}</td>
                          <td title={item.notes || ''}>{shortText(item.notes || '—', 28)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
        <footer className="commerce-dialog-actions">
          <button type="button" className="btn ghost" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn"
            onClick={() => saveMutation.mutate()}
            disabled={invalid || saveMutation.isPending}
          >
            {saveMutation.isPending ? '保存中…' : '保存新版本'}
          </button>
        </footer>
      </section>
    </div>
  );
}

export default function ProductSettingsPage() {
  const { wid } = useParams();
  const queryClient = useQueryClient();
  const sessionQuery = useSessionQuery();
  const [shopId, setShopId] = useState('');
  const [advertiserId, setAdvertiserId] = useState('');
  const [search, setSearch] = useState('');
  const [costFilter, setCostFilter] = useState('all');
  const [editingProduct, setEditingProduct] = useState(null);
  const [editingFlashSale, setEditingFlashSale] = useState(null);
  const [notice, setNotice] = useState(null);

  const contextQuery = useQuery({
    queryKey: ['commerce', 'context', wid],
    queryFn: ({ signal }) => getCommerceContext(wid, { signal }),
    enabled: Boolean(wid),
    staleTime: 5 * 60 * 1000,
  });
  const context = contextQuery.data || {};
  const role = String(sessionQuery.data?.role || '').toLowerCase();
  const canManage = role === 'owner' || role === 'admin';
  const shops = Array.isArray(context.shops) ? context.shops : [];

  useEffect(() => {
    if (shops.length === 0) return;
    setShopId((current) => (
      shops.some((item) => String(item.id) === String(current))
        ? current
        : String(context.default_shop_id || shops[0].id)
    ));
  }, [context.default_shop_id, shops]);

  const advertisers = useMemo(
    () => advertisersForShop(context, shopId),
    [context, shopId],
  );
  useEffect(() => {
    if (advertisers.length === 0) {
      setAdvertiserId('');
      return;
    }
    setAdvertiserId((current) => (
      advertisers.some((item) => String(item.advertiser_id) === String(current))
        ? current
        : String(advertisers[0].advertiser_id)
    ));
  }, [advertisers]);

  const advertiser = advertisers.find(
    (item) => String(item.advertiser_id) === String(advertiserId),
  );
  const timezone = advertiser?.timezone || context.default_reporting_timezone || 'UTC';
  const range = useMemo(() => rangeForPreset('30d', timezone), [timezone]);
  const params = useMemo(
    () => ({
      shop_id: shopId || undefined,
      advertiser_id: advertiserId || undefined,
      ...range,
    }),
    [advertiserId, range, shopId],
  );
  const overviewQuery = useQuery({
    queryKey: ['commerce', 'product-settings', wid, params],
    queryFn: ({ signal }) => getCommerceOverview(wid, params, { signal }),
    enabled: Boolean(wid && shopId && advertiserId),
    staleTime: 60 * 1000,
  });
  const syncStatusQuery = useQuery({
    queryKey: ['commerce', 'sync-status', wid, shopId],
    queryFn: ({ signal }) => getCommerceSyncStatus(
      wid,
      { shop_id: shopId },
      { signal },
    ),
    enabled: Boolean(wid && shopId),
    staleTime: 5 * 1000,
    refetchInterval: (query) => (
      Object.values(query.state.data?.domains || {})
        .some((item) => item.status === 'running')
        ? 2_000
        : 30_000
    ),
  });
  const flashSaleQuery = useQuery({
    queryKey: ['commerce', 'flash-sales', wid, shopId],
    queryFn: ({ signal }) => getFlashSalePolicies(
      wid,
      { shop_id: shopId },
      { signal },
    ),
    enabled: Boolean(wid && shopId),
    staleTime: 30 * 1000,
    refetchInterval: 60 * 1000,
  });
  const flashSaleByProduct = useMemo(
    () => Object.fromEntries(
      (flashSaleQuery.data?.items || []).map((item) => [String(item.product_id), item]),
    ),
    [flashSaleQuery.data?.items],
  );
  const syncInProgress = Object.values(syncStatusQuery.data?.domains || {})
    .some((item) => item.status === 'running');
  const syncMutation = useMutation({
    mutationFn: () => syncCommerceData(wid, {
      shop_id: Number(shopId),
      start_date: range.start_date,
      end_date: range.end_date,
      include_finance: true,
    }),
    onSuccess: async () => {
      setNotice({ type: 'success', text: '商品目录与订单已进入后台同步队列。' });
      await queryClient.invalidateQueries({ queryKey: ['commerce'] });
    },
    onError: (error) => setNotice({ type: 'error', text: errorMessage(error) }),
  });
  const products = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const source = Array.isArray(overviewQuery.data?.products)
      ? overviewQuery.data.products
      : [];
    return source.filter((product) => {
      if (costFilter === 'configured' && !product.current_cost) return false;
      if (costFilter === 'missing' && product.current_cost) return false;
      if (!needle) return true;
      return `${product.title} ${product.product_id}`.toLowerCase().includes(needle);
    });
  }, [costFilter, overviewQuery.data?.products, search]);
  const currency = overviewQuery.data?.scope?.currency || advertiser?.currency || 'USD';
  const configuredCount = (overviewQuery.data?.products || []).filter((item) => item.current_cost).length;
  const totalCount = (overviewQuery.data?.products || []).length;

  return (
    <main className="commerce-page">
      <header className="commerce-page__header">
        <div>
          <h1>商品设置</h1>
          <p>商品资料由 TikTok Shop API 同步；成本采用版本化保存，历史利润不会被新成本覆盖。</p>
        </div>
        <button
          type="button"
          className="btn"
          onClick={() => syncMutation.mutate()}
          disabled={!canManage || !shopId || syncMutation.isPending || syncInProgress}
          title={canManage ? '同步 TikTok Shop 商品和订单' : '仅公司管理员可发起同步'}
        >
          {syncMutation.isPending
            ? '正在提交…'
            : syncInProgress
              ? '后台同步中…'
              : '同步商品与订单'}
        </button>
      </header>

      {notice ? <div className={`alert alert--${notice.type}`}>{notice.text}</div> : null}
      {contextQuery.error ? <div className="alert alert--error">{errorMessage(contextQuery.error)}</div> : null}
      {overviewQuery.error ? <div className="alert alert--error">{errorMessage(overviewQuery.error)}</div> : null}
      {flashSaleQuery.error ? <div className="alert alert--error">{errorMessage(flashSaleQuery.error)}</div> : null}

      <section className="commerce-scope commerce-scope--products">
        <label>
          <span>店铺</span>
          <select value={shopId} onChange={(event) => setShopId(event.target.value)}>
            {shops.map((shop) => <option key={shop.id} value={shop.id}>{shop.name}</option>)}
          </select>
        </label>
        <label>
          <span>广告主</span>
          <select value={advertiserId} onChange={(event) => setAdvertiserId(event.target.value)}>
            {advertisers.map((item) => (
              <option key={item.advertiser_id} value={item.advertiser_id}>{item.name}</option>
            ))}
          </select>
        </label>
        <div className="commerce-product-coverage">
          <span>成本覆盖</span>
          <strong>{configuredCount} / {totalCount}</strong>
          <small>已配置商品</small>
        </div>
      </section>

      <section className="commerce-section">
        <div className="commerce-toolbar">
          <div>
            <h2>商品与成本</h2>
            <p>利润预览使用近 30 日实际订单和广告消耗。</p>
          </div>
          <div className="commerce-toolbar__controls">
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索商品名称或 ID"
              aria-label="搜索商品"
            />
            <select value={costFilter} onChange={(event) => setCostFilter(event.target.value)} aria-label="成本状态">
              <option value="all">全部成本状态</option>
              <option value="configured">已配置成本</option>
              <option value="missing">待配置成本</option>
            </select>
          </div>
        </div>

        {contextQuery.isLoading || overviewQuery.isLoading ? (
          <Loading text="正在读取商品目录…" />
        ) : shops.length === 0 ? (
          <div className="commerce-empty">尚未连接 TikTok Shop。</div>
        ) : (
          <div className="commerce-table-wrap">
            <table className="commerce-table commerce-products-table">
              <thead>
                <tr>
                  <th>商品</th>
                  <th>售价</th>
                  <th>库存</th>
                  <th>当前商品成本</th>
                  <th>费率合计</th>
                  <th>近 30 日成交</th>
                  <th>广告消耗</th>
                  <th>贡献利润</th>
                  <th>闪购自动续期</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {products.length === 0 ? (
                  <tr><td colSpan={10}><div className="commerce-empty">没有符合筛选条件的商品。</div></td></tr>
                ) : products.map((product) => {
                  const cost = product.current_cost;
                  const flashPolicy = flashSaleByProduct[String(product.product_id)];
                  const flashState = flashSaleStatus(flashPolicy);
                  const rate = cost
                    ? Number(cost.platform_fee_rate || 0)
                      + Number(cost.payment_fee_rate || 0)
                      + Number(cost.affiliate_commission_rate || 0)
                      + Number(cost.expected_refund_rate || 0)
                    : null;
                  return (
                    <tr key={product.product_id}>
                      <td>
                        <div className="commerce-product-cell">
                          {product.image_url ? <img src={product.image_url} alt="" loading="lazy" /> : <span className="commerce-image-placeholder">无图</span>}
                          <div>
                            <strong title={product.title}>{shortText(product.title, 48)}</strong>
                            <small>ID {product.product_id}</small>
                            <span className={`commerce-status ${product.mapping_status === 'mapped' ? 'commerce-status--success' : 'commerce-status--warning'}`}>
                              {product.mapping_status === 'mapped' ? '已关联广告商品' : '待关联广告商品'}
                            </span>
                          </div>
                        </div>
                      </td>
                      <td>{formatMoney(product.sale_price, product.currency || currency)}</td>
                      <td>{product.inventory_quantity ?? 0}</td>
                      <td>{cost ? formatMoney(cost.unit_cost, cost.currency) : '未配置'}</td>
                      <td>{formatPercent(rate)}</td>
                      <td>{formatMoney(product.actual_sales, currency)}</td>
                      <td>{formatMoney(product.ad_spend, currency)}</td>
                      <td className={Number(product.contribution_profit) < 0 ? 'commerce-negative' : ''}>
                        {product.contribution_profit == null ? '待补成本' : formatMoney(product.contribution_profit, currency)}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="commerce-flash-sale-cell"
                          onClick={() => canManage && setEditingFlashSale(product)}
                          disabled={!canManage}
                          title={canManage ? '设置闪购活动价' : '仅公司管理员可修改'}
                        >
                          <span className={`commerce-flash-state commerce-flash-state--${flashState.tone}`}>
                            {flashState.text}
                          </span>
                          {flashPolicy?.enabled ? (
                            <>
                              <strong>
                                {formatMoney(
                                  flashPolicy.activity_price_amount,
                                  flashPolicy.currency,
                                )}
                              </strong>
                              <small>覆盖至 {shopLocalTime(flashPolicy.current_end_at)}</small>
                            </>
                          ) : <small>点击设置活动价</small>}
                        </button>
                      </td>
                      <td>
                        {canManage ? (
                          <button type="button" className="btn sm ghost" onClick={() => setEditingProduct(product)}>
                            {cost ? '更新成本' : '配置成本'}
                          </button>
                        ) : <span className="commerce-muted">只读</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {editingProduct && canManage ? (
        <CostDialog
          product={editingProduct}
          shopId={shopId}
          workspaceId={wid}
          onClose={() => setEditingProduct(null)}
          onSaved={async () => {
            setEditingProduct(null);
            setNotice({ type: 'success', text: '成本新版本已保存，利润数据已重新计算。' });
            await queryClient.invalidateQueries({ queryKey: ['commerce'] });
          }}
        />
      ) : null}
      {editingFlashSale && canManage ? (
        <FlashSaleDialog
          product={editingFlashSale}
          policy={flashSaleByProduct[String(editingFlashSale.product_id)]}
          shopId={shopId}
          workspaceId={wid}
          onClose={() => setEditingFlashSale(null)}
          onSaved={async () => {
            setEditingFlashSale(null);
            setNotice({ type: 'success', text: '闪购活动价已保存，后台正在校验冲突并安排续期。' });
            await queryClient.invalidateQueries({ queryKey: ['commerce', 'flash-sales'] });
          }}
        />
      ) : null}
    </main>
  );
}
