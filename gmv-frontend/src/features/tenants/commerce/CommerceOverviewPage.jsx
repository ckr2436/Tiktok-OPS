import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';

import Loading from '@/components/ui/Loading.jsx';
import {
  getCommerceContext,
  getCommerceOverview,
  getCommerceSyncStatus,
  syncCommerceData,
} from './api.js';
import {
  advertisersForShop,
  errorMessage,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  formatRatio,
  rangeForPreset,
  rangeLabel,
  shortText,
} from './commerceUtils.js';
import './commerce.css';

const RANGE_OPTIONS = [
  { value: 'today', label: '今日' },
  { value: 'yesterday', label: '昨日' },
  { value: '7d', label: '近 7 日' },
  { value: '30d', label: '近 30 日' },
  { value: 'custom', label: '自定义' },
];

function TrendChart({ rows, currency }) {
  const points = Array.isArray(rows) ? rows : [];
  if (points.length === 0) {
    return <div className="commerce-empty">所选范围暂无趋势数据。</div>;
  }
  const width = 920;
  const height = 240;
  const padding = { left: 54, right: 18, top: 18, bottom: 38 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const maxValue = Math.max(
    1,
    ...points.flatMap((item) => [
      Number(item.actual_sales || 0),
      Number(item.ad_spend || 0),
    ]),
  );
  const x = (index) => (
    padding.left
    + (points.length === 1 ? chartWidth / 2 : (index / (points.length - 1)) * chartWidth)
  );
  const y = (value) => padding.top + chartHeight - (Number(value || 0) / maxValue) * chartHeight;
  const path = (key) => points
    .map((item, index) => `${index === 0 ? 'M' : 'L'} ${x(index)} ${y(item[key])}`)
    .join(' ');
  const labelIndexes = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];

  return (
    <div className="commerce-chart">
      <div className="commerce-chart__legend" aria-hidden="true">
        <span><i className="commerce-dot commerce-dot--sales" />订单实付</span>
        <span><i className="commerce-dot commerce-dot--spend" />广告消耗</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="订单实付与广告消耗趋势">
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
          const gridY = padding.top + chartHeight * ratio;
          const value = maxValue * (1 - ratio);
          return (
            <g key={ratio}>
              <line
                x1={padding.left}
                x2={width - padding.right}
                y1={gridY}
                y2={gridY}
                className="commerce-chart__grid"
              />
              <text x={padding.left - 8} y={gridY + 4} textAnchor="end">
                {formatMoney(value, currency).replace(/\.\d{2}$/, '')}
              </text>
            </g>
          );
        })}
        <path d={path('actual_sales')} className="commerce-chart__line commerce-chart__line--sales" />
        <path d={path('ad_spend')} className="commerce-chart__line commerce-chart__line--spend" />
        {points.map((item, index) => (
          <g key={item.date}>
            <circle cx={x(index)} cy={y(item.actual_sales)} r="3" className="commerce-chart__point commerce-chart__point--sales">
              <title>{`${item.date} 订单实付 ${formatMoney(item.actual_sales, currency)}`}</title>
            </circle>
            <circle cx={x(index)} cy={y(item.ad_spend)} r="3" className="commerce-chart__point commerce-chart__point--spend">
              <title>{`${item.date} 广告消耗 ${formatMoney(item.ad_spend, currency)}`}</title>
            </circle>
          </g>
        ))}
        {labelIndexes.map((index) => (
          <text
            key={points[index].date}
            x={x(index)}
            y={height - 12}
            textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}
          >
            {points[index].date.slice(5)}
          </text>
        ))}
      </svg>
    </div>
  );
}

function Kpi({ label, value, detail, tone = '' }) {
  return (
    <div className={`commerce-kpi ${tone ? `commerce-kpi--${tone}` : ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

export default function CommerceOverviewPage() {
  const { wid } = useParams();
  const queryClient = useQueryClient();
  const [shopId, setShopId] = useState('');
  const [advertiserId, setAdvertiserId] = useState('');
  const [preset, setPreset] = useState('7d');
  const [customRange, setCustomRange] = useState({ start_date: '', end_date: '' });
  const [notice, setNotice] = useState(null);

  const contextQuery = useQuery({
    queryKey: ['commerce', 'context', wid],
    queryFn: ({ signal }) => getCommerceContext(wid, { signal }),
    enabled: Boolean(wid),
    staleTime: 5 * 60 * 1000,
  });
  const context = contextQuery.data || {};
  const shops = Array.isArray(context.shops) ? context.shops : [];

  useEffect(() => {
    if (shops.length === 0) return;
    setShopId((current) => {
      if (shops.some((item) => String(item.id) === String(current))) return current;
      return String(context.default_shop_id || shops[0].id);
    });
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
    setAdvertiserId((current) => {
      if (advertisers.some((item) => String(item.advertiser_id) === String(current))) {
        return current;
      }
      const preferred = String(context.default_advertiser_id || '');
      return advertisers.some((item) => String(item.advertiser_id) === preferred)
        ? preferred
        : String(advertisers[0].advertiser_id);
    });
  }, [advertisers, context.default_advertiser_id]);

  const selectedShop = shops.find((item) => String(item.id) === String(shopId));
  const selectedAdvertiser = advertisers.find(
    (item) => String(item.advertiser_id) === String(advertiserId),
  );
  const reportingTimezone = selectedAdvertiser?.timezone
    || context.default_reporting_timezone
    || 'UTC';
  const selectedRange = useMemo(
    () => (preset === 'custom' ? customRange : rangeForPreset(preset, reportingTimezone)),
    [customRange, preset, reportingTimezone],
  );
  const validRange = Boolean(
    selectedRange.start_date
    && selectedRange.end_date
    && selectedRange.start_date <= selectedRange.end_date,
  );
  const overviewParams = useMemo(
    () => ({
      shop_id: shopId || undefined,
      advertiser_id: advertiserId || undefined,
      start_date: selectedRange.start_date || undefined,
      end_date: selectedRange.end_date || undefined,
    }),
    [
      advertiserId,
      selectedRange.end_date,
      selectedRange.start_date,
      shopId,
    ],
  );
  const overviewQuery = useQuery({
    queryKey: ['commerce', 'overview', wid, overviewParams],
    queryFn: ({ signal }) => getCommerceOverview(wid, overviewParams, { signal }),
    enabled: Boolean(wid && shopId && advertiserId && validRange),
    staleTime: 45 * 1000,
    refetchInterval: 60 * 1000,
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
    refetchInterval: (query) => {
      const domains = Object.values(query.state.data?.domains || {});
      return domains.some((item) => item.status === 'running') ? 2_000 : 30_000;
    },
  });
  const syncDomains = syncStatusQuery.data?.domains || {};
  const syncInProgress = Object.values(syncDomains).some(
    (item) => item.status === 'running',
  );
  const syncMutation = useMutation({
    mutationFn: () => syncCommerceData(wid, {
      shop_id: Number(shopId),
      start_date: selectedRange.start_date,
      end_date: selectedRange.end_date,
      include_finance: true,
    }),
    onSuccess: async () => {
      setNotice({ type: 'success', text: '同步任务已进入后台队列，页面会在完成后自动更新。' });
      await queryClient.invalidateQueries({ queryKey: ['commerce'] });
    },
    onError: (error) => setNotice({ type: 'error', text: errorMessage(error) }),
  });

  const data = overviewQuery.data || {};
  const summary = data.summary || {};
  const health = data.data_health || {};
  const finance = health.finance || {};
  const currency = data.scope?.currency || selectedAdvertiser?.currency || 'USD';
  const products = Array.isArray(data.products) ? data.products : [];

  if (contextQuery.isLoading) {
    return <Loading text="正在加载经营数据…" />;
  }

  if (contextQuery.error) {
    return <div className="alert alert--error">{errorMessage(contextQuery.error)}</div>;
  }

  if (shops.length === 0) {
    return (
      <main className="commerce-page">
        <header className="commerce-page__header">
          <div>
            <h1>数据总览</h1>
            <p>连接 TikTok Shop 后，订单、商品和广告数据将在这里统一核算。</p>
          </div>
        </header>
        <section className="commerce-empty commerce-empty--action">
          <strong>尚未连接 TikTok Shop</strong>
          <span>完成卖家授权后即可使用 API 订单和商品利润分析。</span>
          <Link className="btn" to={`/tenants/${wid}/tiktok-shop`}>前往授权</Link>
        </section>
      </main>
    );
  }

  return (
    <main className="commerce-page">
      <header className="commerce-page__header">
        <div>
          <h1>数据总览</h1>
          <p>统一查看 TikTok Shop 实际成交、GMV Max 广告消耗与商品贡献利润。</p>
        </div>
        <div className="commerce-page__actions">
          <button
            type="button"
            className="btn ghost"
            onClick={() => overviewQuery.refetch()}
            disabled={overviewQuery.isFetching}
          >
            {overviewQuery.isFetching ? '刷新中…' : '刷新页面'}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => syncMutation.mutate()}
            disabled={!validRange || syncMutation.isPending || syncInProgress}
          >
            {syncMutation.isPending
              ? '正在提交…'
              : syncInProgress
                ? '后台同步中…'
                : '同步最新数据'}
          </button>
        </div>
      </header>

      {notice ? <div className={`alert alert--${notice.type}`}>{notice.text}</div> : null}
      {overviewQuery.error ? (
        <div className="alert alert--error">{errorMessage(overviewQuery.error)}</div>
      ) : null}

      <section className="commerce-scope" aria-label="数据范围">
        <label>
          <span>店铺</span>
          <select value={shopId} onChange={(event) => setShopId(event.target.value)}>
            {shops.map((shop) => (
              <option key={shop.id} value={shop.id}>{shop.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span>广告主</span>
          <select
            value={advertiserId}
            onChange={(event) => setAdvertiserId(event.target.value)}
            disabled={advertisers.length === 0}
          >
            {advertisers.map((advertiser) => (
              <option key={advertiser.advertiser_id} value={advertiser.advertiser_id}>
                {advertiser.name}
              </option>
            ))}
          </select>
        </label>
        <div className="commerce-scope__timezone">
          <span>订单源时区</span>
          <strong>UTC-8（固定）</strong>
          <small>{selectedShop?.timezone_source === 'merchant_confirmed_fixed_utc_minus_8' ? '已确认' : '平台默认'}</small>
        </div>
        <div className="commerce-scope__timezone">
          <span>报表时区</span>
          <strong>{reportingTimezone}</strong>
          <small>由广告户 API 获取</small>
        </div>
      </section>

      <section className="commerce-range" aria-label="统计日期">
        <div className="commerce-segments">
          {RANGE_OPTIONS.map((option) => (
            <button
              type="button"
              key={option.value}
              className={preset === option.value ? 'is-active' : ''}
              onClick={() => setPreset(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        {preset === 'custom' ? (
          <div className="commerce-date-inputs">
            <input
              type="date"
              value={customRange.start_date}
              onChange={(event) => setCustomRange((value) => ({ ...value, start_date: event.target.value }))}
              aria-label="开始日期"
            />
            <span>至</span>
            <input
              type="date"
              value={customRange.end_date}
              onChange={(event) => setCustomRange((value) => ({ ...value, end_date: event.target.value }))}
              aria-label="结束日期"
            />
          </div>
        ) : null}
        <strong className="commerce-range__label">
          {rangeLabel(selectedRange.start_date, selectedRange.end_date)}
        </strong>
      </section>

      {overviewQuery.isLoading ? <Loading text="正在核算订单、广告和成本…" /> : (
        <>
          <section className="commerce-kpis" aria-label="经营概览">
            <Kpi
              label="订单实付成交"
              value={formatMoney(summary.order_paid_sales ?? summary.actual_net_sales, currency)}
              detail={`${formatNumber(summary.orders)} 笔有效订单`}
            />
            <Kpi
              label="广告消耗"
              value={formatMoney(summary.ad_spend, currency)}
              detail={`归因 GMV ${formatMoney(summary.ad_attributed_gmv, currency)}`}
            />
            <Kpi
              label="预估贡献利润"
              value={summary.contribution_profit == null ? '待补成本' : formatMoney(summary.contribution_profit, currency)}
              detail={summary.contribution_margin == null ? '补全成本后按版本核算' : `预估贡献率 ${formatPercent(summary.contribution_margin)}`}
              tone={summary.contribution_profit != null && Number(summary.contribution_profit) < 0 ? 'danger' : 'success'}
            />
            <Kpi
              label="整体投产"
              value={formatRatio(summary.blended_mer)}
              detail={`广告归因 ROAS ${formatRatio(summary.attributed_roas)}`}
            />
            <Kpi
              label="自然成交占比"
              value={formatPercent(summary.organic_revenue_share)}
              detail={`估算自然成交 ${formatMoney(summary.organic_revenue_estimate, currency)}`}
            />
          </section>

          {Math.abs(Number(summary.order_reconciliation_delta || 0)) > 0.01 ? (
            <div className="alert alert--warning">
              商品明细与订单实付仍有 {formatMoney(
                summary.order_reconciliation_delta,
                currency,
              )} 未完成分摊，请先同步订单明细后再进行盈亏判断。
            </div>
          ) : null}
          {Number(summary.orders || 0) > 0 && Number(finance.coverage_ratio || 0) < 1 ? (
            <div className="alert alert--warning">
              结算 API 当前覆盖 {formatPercent(finance.coverage_ratio || 0)} 的订单。
              页面贡献利润按订单实付和成本版本估算，最终结算请以 TikTok Shop 财务数据为准。
            </div>
          ) : null}

          <section className="commerce-section">
            <div className="commerce-section__header">
              <div>
                <h2>订单实付与消耗趋势</h2>
                <p>{rangeLabel(selectedRange.start_date, selectedRange.end_date)}，按广告户时区汇总。</p>
              </div>
            </div>
            <TrendChart rows={data.trends} currency={currency} />
          </section>

          <section className="commerce-section">
            <div className="commerce-section__header">
              <div>
                <h2>商品盈亏</h2>
                <p>订单实付来自 Shop API；广告数据来自 GMV Max PRODUCT 维度，不重复分摊系列消耗。</p>
              </div>
              <Link className="btn ghost" to={`/tenants/${wid}/products`}>管理商品成本</Link>
            </div>
            <div className="commerce-table-wrap">
              <table className="commerce-table">
                <thead>
                  <tr>
                    <th>商品</th>
                    <th>订单实付分摊</th>
                    <th>订单</th>
                    <th>广告消耗</th>
                    <th>归因 GMV</th>
                    <th>ROAS</th>
                    <th>贡献利润</th>
                    <th>贡献率</th>
                    <th>成本状态</th>
                  </tr>
                </thead>
                <tbody>
                  {products.length === 0 ? (
                    <tr><td colSpan={9}><div className="commerce-empty">暂无商品数据。</div></td></tr>
                  ) : products.map((product) => (
                    <tr key={product.product_id}>
                      <td>
                        <div className="commerce-product-cell">
                          {product.image_url ? <img src={product.image_url} alt="" loading="lazy" /> : <span className="commerce-image-placeholder">无图</span>}
                          <div>
                            <strong title={product.title}>{shortText(product.title, 44)}</strong>
                            <small>ID {product.product_id}</small>
                          </div>
                        </div>
                      </td>
                      <td>{formatMoney(product.actual_sales, currency)}</td>
                      <td>{formatNumber(product.orders)}</td>
                      <td>{formatMoney(product.ad_spend, currency)}</td>
                      <td>{formatMoney(product.ad_attributed_gmv, currency)}</td>
                      <td>{formatRatio(product.roas)}</td>
                      <td>{product.contribution_profit == null ? '—' : formatMoney(product.contribution_profit, currency)}</td>
                      <td>{formatPercent(product.contribution_margin)}</td>
                      <td>
                        <span className={`commerce-status ${product.current_cost ? 'commerce-status--success' : 'commerce-status--warning'}`}>
                          {product.current_cost ? '已配置' : '待配置'}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="commerce-data-health">
            <div>
              <strong>订单 API</strong>
              <span>最近同步 {formatDateTime(health.last_order_sync_at)}</span>
            </div>
            <div>
              <strong>商品 API</strong>
              <span>最近同步 {formatDateTime(health.last_product_sync_at)}</span>
            </div>
            <div>
              <strong>结算 API</strong>
              <span>
                {formatNumber(finance.covered_orders)} / {formatNumber(summary.orders)} 笔已覆盖
              </span>
            </div>
            <div>
              <strong>广告报表</strong>
              <span>最近同步 {formatDateTime(health.last_ad_sync_at)}</span>
            </div>
            <div>
              <strong>商品映射</strong>
              <span>{formatNumber(health.product_mapping?.mapped)} / {formatNumber(health.product_mapping?.total)} 已关联</span>
            </div>
            <div>
              <strong>后台同步</strong>
              <span>
                {syncInProgress
                  ? '订单、商品与财务数据同步中'
                  : Object.values(syncDomains).some((item) => item.status === 'failed')
                    ? '存在失败任务，请重新同步'
                    : '运行正常'}
              </span>
            </div>
          </section>
        </>
      )}
    </main>
  );
}
