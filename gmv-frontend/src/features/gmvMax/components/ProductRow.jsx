import { memo } from 'react';

function formatPriceRange(minPrice, maxPrice, currency) {
  if (!minPrice && !maxPrice) return '—';
  if (minPrice && !maxPrice) return `${currency || ''}${minPrice}`;
  if (!minPrice && maxPrice) return `${currency || ''}${maxPrice}`;
  if (minPrice === maxPrice) return `${currency || ''}${minPrice}`;
  return `${currency || ''}${minPrice} - ${currency || ''}${maxPrice}`;
}

function StatusBadges({ product = {} }) {
  const badges = [];

  if (product.status) {
    badges.push({
      key: 'status',
      label: product.status === 'AVAILABLE' ? '可投放' : '不可投放',
      tone: product.status === 'AVAILABLE' ? 'available' : 'not-available',
    });
  }

  if (product.gmvMaxAdsStatus) {
    badges.push({
      key: 'gmv',
      label: product.gmvMaxAdsStatus === 'OCCUPIED' ? '已占用' : '未占用',
      tone: product.gmvMaxAdsStatus === 'OCCUPIED' ? 'occupied' : 'unoccupied',
    });
  }

  if (product.isRunningCustomShopAds) {
    badges.push({ key: 'sa', label: 'Running SA', tone: 'sa' });
  }

  return (
    <div className="gmv-product-row__badges" aria-label="商品标签">
      {badges.map((badge) => (
        <span key={badge.key} className={`gmv-badge gmv-badge--${badge.tone}`}>
          {badge.label}
        </span>
      ))}
    </div>
  );
}

const ProductRow = memo(function ProductRow({
  product = {},
  onOpenDetail,
  onViewOccupied,
  onCreatePlan,
  onOpenAutomation,
}) {
  return (
    <div
      className="gmv-product-row"
      role="button"
      tabIndex={0}
      onClick={() => onOpenDetail?.(product)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onOpenDetail?.(product);
        }
      }}
    >
      <img
        src={product.imageUrl || 'https://lf16-tiktok-business.myfilecdn.com/obj/ad-mcs-sg/marketing-resource-center/placeholder.png'}
        alt={product.title || '商品缩略图'}
        className="gmv-product-row__thumb"
      />
      <div className="gmv-product-row__info">
        <div className="gmv-product-row__title">{product.title || '未命名商品'}</div>
        <div className="gmv-product-row__meta">
          <span>{formatPriceRange(product.minPrice, product.maxPrice, product.currency)}</span>
          {product.historicalSales ? <span>近 30 天销量 {product.historicalSales}</span> : null}
        </div>
        <StatusBadges product={product} />
      </div>
      <div className="gmv-product-row__actions">
        {product.gmvMaxAdsStatus === 'OCCUPIED' ? (
          <button
            type="button"
            className="btn ghost"
            onClick={(event) => {
              event.stopPropagation();
              onViewOccupied?.(product);
            }}
          >
            查看占用详情
          </button>
        ) : null}
        {product.gmvMaxAdsStatus === 'UNOCCUPIED' ? (
          <button
            type="button"
            className="btn ghost"
            onClick={(event) => {
              event.stopPropagation();
              onCreatePlan?.(product);
            }}
          >
            新建 GMV Max 计划
          </button>
        ) : null}
        <button
          type="button"
          className="btn ghost"
          onClick={(event) => {
            event.stopPropagation();
            onOpenAutomation?.(product);
          }}
        >
          自动化设置
        </button>
      </div>
    </div>
  );
});

export default ProductRow;
