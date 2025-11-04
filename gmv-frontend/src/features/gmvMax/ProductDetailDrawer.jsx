import { useEffect, useMemo, useState } from 'react';

function formatRange(product) {
  if (!product) return '—';
  const { minPrice, maxPrice, currency } = product;
  if (!minPrice && !maxPrice) return '—';
  if (minPrice && !maxPrice) return `${currency || ''}${minPrice}`;
  if (!minPrice && maxPrice) return `${currency || ''}${maxPrice}`;
  if (minPrice === maxPrice) return `${currency || ''}${minPrice}`;
  return `${currency || ''}${minPrice} - ${currency || ''}${maxPrice}`;
}

export default function ProductDetailDrawer({ open = false, product = null, onClose }) {
  const [showAutomation, setShowAutomation] = useState(false);

  useEffect(() => {
    if (!open) {
      setShowAutomation(false);
      return undefined;
    }
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  const priceRange = useMemo(() => formatRange(product), [product]);

  if (!open) return null;

  return (
    <div
      className="product-detail-drawer-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose?.();
        }
      }}
    >
      <aside className="product-detail-drawer" role="dialog" aria-modal="true">
        <header className="product-detail-drawer__header">
          <div>
            <h3>商品详情</h3>
            <p className="product-detail-drawer__subtitle">SKU 信息暂未接入，敬请期待。</p>
          </div>
          <button type="button" className="product-detail-drawer__close" onClick={onClose}>
            ×
          </button>
        </header>
        <div className={`product-detail-drawer__body${showAutomation ? ' automation-open' : ''}`}>
          <div className="product-detail-drawer__main">
            <div className="product-detail-drawer__preview">
              <img
                src={
                  product?.imageUrl
                  || product?.productImageUrl
                  || 'https://lf16-tiktok-business.myfilecdn.com/obj/ad-mcs-sg/marketing-resource-center/placeholder.png'
                }
                alt={product?.title || '商品图片'}
              />
            </div>
            <div className="product-detail-drawer__info">
              <h4>{product?.title || '未命名商品'}</h4>
              <div className="product-detail-drawer__meta">
                <span>商品组 ID：{product?.itemGroupId || '—'}</span>
                <span>价格区间：{priceRange}</span>
                <span>类目：{product?.category || '待接入'}</span>
                <span>近 30 天销量：{product?.historicalSales || '—'}</span>
                <span>状态：{product?.status || '—'}</span>
                <span>GMV Max 占用：{product?.gmvMaxAdsStatus || '—'}</span>
              </div>
              <div className="product-detail-drawer__section">
                <h5>SKU 列表（待接入）</h5>
                <div className="product-detail-drawer__placeholder">
                  TODO：接入 SKU 维度接口后展示规格、库存及定价详情。
                </div>
              </div>
              <div className="product-detail-drawer__section">
                <h5>历史表现（待接入）</h5>
                <div className="product-detail-drawer__placeholder">GMV、曝光、转化等数据接入中。</div>
              </div>
              <button
                type="button"
                className="btn"
                onClick={() => setShowAutomation(true)}
                disabled={showAutomation}
              >
                自动化设置
              </button>
            </div>
          </div>
          {showAutomation ? (
            <aside className="product-detail-drawer__automation" aria-label="自动化设置">
              <header>
                <h4>自动化设置（占位）</h4>
                <button type="button" className="product-detail-drawer__close" onClick={() => setShowAutomation(false)}>
                  ×
                </button>
              </header>
              <div className="product-detail-drawer__automation-body">
                <label className="form-label" htmlFor="automation-bid-threshold">出价阈值</label>
                <input
                  id="automation-bid-threshold"
                  className="form-input"
                  placeholder="例如：¥50"
                  readOnly
                  value="TODO"
                />
                <label className="form-label" htmlFor="automation-budget">预算上限</label>
                <input id="automation-budget" className="form-input" placeholder="例如：¥2000" readOnly value="TODO" />
                <label className="form-label" htmlFor="automation-strategy">占用切换策略</label>
                <input id="automation-strategy" className="form-input" placeholder="策略占位" readOnly value="TODO" />
                <div className="product-detail-drawer__placeholder">
                  TODO：表单提交与校验逻辑待接入。
                </div>
                <button type="button" className="btn ghost" onClick={() => setShowAutomation(false)}>
                  保存（占位）
                </button>
              </div>
            </aside>
          ) : null}
        </div>
      </aside>
    </div>
  );
}

