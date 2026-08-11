import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import Loading from '@/components/ui/Loading.jsx';
import { getCommerceOrderSummary } from '@/features/tenants/commerce/api.js';
import { formatDateTime } from '@/features/tenants/commerce/commerceUtils.js';
import { formatError, formatMoney } from './helpers.js';

function zonedDateString(date, timeZone) {
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: timeZone || 'UTC',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
  const parts = Object.fromEntries(
    formatter
      .formatToParts(date)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function defaultRange(timeZone) {
  const end = zonedDateString(new Date(), timeZone);
  const start = new Date(`${end}T12:00:00Z`);
  start.setUTCDate(start.getUTCDate() - 29);
  return { start: start.toISOString().slice(0, 10), end };
}

export default function OrderIntelligencePanel({
  workspaceId,
  advertiserId,
  storeId,
  advertiserTimezone,
  enabled,
}) {
  const initialRange = useMemo(
    () => defaultRange(advertiserTimezone),
    [advertiserTimezone],
  );
  const [range, setRange] = useState(initialRange);

  useEffect(() => {
    setRange(initialRange);
  }, [initialRange]);

  const params = useMemo(
    () => ({
      advertiser_id: advertiserId || undefined,
      store_id: storeId || undefined,
      start_date: range.start,
      end_date: range.end,
    }),
    [advertiserId, range.end, range.start, storeId],
  );
  const summaryQuery = useQuery({
    queryKey: ['commerce', 'gmvmax-order-summary', workspaceId, params],
    queryFn: ({ signal }) => getCommerceOrderSummary(workspaceId, params, { signal }),
    enabled: Boolean(
      enabled
      && workspaceId
      && advertiserId
      && storeId
      && range.start
      && range.end
      && range.start <= range.end,
    ),
    staleTime: 45 * 1000,
    refetchInterval: 60 * 1000,
  });
  const summary = summaryQuery.data || {};
  const hourly = Array.isArray(summary.hourly_profile) ? summary.hourly_profile : [];
  const products = Array.isArray(summary.products) ? summary.products : [];
  const maxOrders = Math.max(1, ...hourly.map((item) => Number(item.orders || 0)));

  return (
    <section className="gmvmax-card gmvmax-order-intelligence">
      <header className="gmvmax-card__header">
        <div>
          <h2>订单与时段智能</h2>
          <p>
            订单由 TikTok Shop API 自动同步。源时间固定按 UTC-8 解释，
            再按广告户时区切分日期和出单时段。
          </p>
        </div>
        <div className="gmvmax-card__header-actions">
          <span className="gmvmax-muted-text">
            最近同步：{formatDateTime(summary.last_synced_at)}
          </span>
          <button
            type="button"
            className="gmvmax-button"
            onClick={() => summaryQuery.refetch()}
            disabled={summaryQuery.isFetching}
          >
            {summaryQuery.isFetching ? '刷新中…' : '刷新订单'}
          </button>
        </div>
      </header>
      <div className="gmvmax-card__body">
        <div className="gmvmax-order-source-strip">
          <span><strong>来源</strong>TikTok Shop API</span>
          <span><strong>订单源时区</strong>UTC-8（固定）</span>
          <span><strong>报表时区</strong>{summary.range?.timezone || advertiserTimezone || '待同步'}</span>
          <span><strong>隐私</strong>仅保存经营字段，不向 Hermes 提供买家身份信息</span>
        </div>

        <div className="gmvmax-order-range">
          <label>
            开始日期
            <input
              type="date"
              value={range.start}
              onChange={(event) => setRange((value) => ({ ...value, start: event.target.value }))}
            />
          </label>
          <span>至</span>
          <label>
            结束日期
            <input
              type="date"
              value={range.end}
              onChange={(event) => setRange((value) => ({ ...value, end: event.target.value }))}
            />
          </label>
        </div>

        {summaryQuery.isLoading ? <Loading text="正在读取 Shop API 订单…" /> : null}
        {summaryQuery.error ? (
          <div className="gmvmax-status-banner gmvmax-status-banner--error">
            {formatError(summaryQuery.error)}
          </div>
        ) : null}

        {!summaryQuery.isLoading && !summaryQuery.error ? (
          <>
            <div className="gmvmax-overview-summary gmvmax-order-summary">
              <div className="gmvmax-overview-summary__item">
                <span>有效订单</span>
                <strong>{summary.order_count ?? 0}</strong>
              </div>
              <div className="gmvmax-overview-summary__item">
                <span>净成交</span>
                <strong>{formatMoney(summary.net_revenue || 0)}</strong>
              </div>
              <div className="gmvmax-overview-summary__item">
                <span>取消订单</span>
                <strong>{summary.cancelled_order_count ?? 0}</strong>
              </div>
              <div className="gmvmax-overview-summary__item">
                <span>平均客单</span>
                <strong>{formatMoney(summary.average_order_value || 0)}</strong>
              </div>
            </div>

            {hourly.length > 0 ? (
              <div className="gmvmax-order-profile">
                <div className="gmvmax-order-profile__heading">
                  <h3>广告户时区出单分布</h3>
                  <span>{range.start} 至 {range.end}</span>
                </div>
                <div className="gmvmax-order-hours" aria-label="24 小时订单分布">
                  {hourly.map((item) => (
                    <div
                      className="gmvmax-order-hour"
                      key={item.hour}
                      title={`${item.label}：${item.orders} 单，建议节奏系数 ${item.delivery_multiplier}`}
                    >
                      <div className="gmvmax-order-hour__track">
                        <span
                          style={{
                            height: `${Math.max(
                              3,
                              (Number(item.orders || 0) / maxOrders) * 100,
                            )}%`,
                          }}
                        />
                      </div>
                      <strong>{item.orders}</strong>
                      <small>{String(item.hour).padStart(2, '0')}</small>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <p className="gmvmax-placeholder">当前日期范围暂无有效订单。</p>
            )}

            {products.length > 0 ? (
              <div className="gmvmax-order-product-table">
                <h3>商品订单</h3>
                <table>
                  <thead>
                    <tr>
                      <th>商品</th>
                      <th>订单</th>
                      <th>件数</th>
                      <th>商品成交</th>
                      <th>平均售价</th>
                    </tr>
                  </thead>
                  <tbody>
                    {products.map((item) => (
                      <tr key={item.product_id}>
                        <td title={item.product_name}>{item.product_name || item.product_id}</td>
                        <td>{item.orders}</td>
                        <td>{item.quantity}</td>
                        <td>{formatMoney(item.item_revenue || 0)}</td>
                        <td>{formatMoney(item.average_item_price || 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </section>
  );
}
