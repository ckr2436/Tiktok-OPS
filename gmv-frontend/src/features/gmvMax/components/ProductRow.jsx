import { memo, useMemo } from 'react';

const PLACEHOLDER =
  'https://lf16-tiktok-business.myfilecdn.com/obj/ad-mcs-sg/marketing-resource-center/placeholder.png';

function formatPriceRange(minPrice, maxPrice, currency) {
  if (!minPrice && !maxPrice) return '—';
  if (minPrice && !maxPrice) return `${currency || ''}${minPrice}`;
  if (!minPrice && maxPrice) return `${currency || ''}${maxPrice}`;
  if (minPrice === maxPrice) return `${currency || ''}${minPrice}`;
  return `${currency || ''}${minPrice} - ${currency || ''}${maxPrice}`;
}

function StatusBadges({ product = {} }) {
  const badges = useMemo(() => {
    const list = [];
    if (product.status) {
      list.push({
        key: 'status',
        label: product.status === 'AVAILABLE' ? '可投放' : '不可投放',
        className: product.status === 'AVAILABLE' ? 'badge badge-ok' : 'badge badge-muted',
      });
    }

    if (product.gmvMaxAdsStatus) {
      list.push({
        key: 'gmv',
        label: product.gmvMaxAdsStatus === 'OCCUPIED' ? '已占用' : '未占用',
        className:
          product.gmvMaxAdsStatus === 'OCCUPIED' ? 'badge badge-warn' : 'badge badge-ok',
      });
    }

    if (product.isRunningCustomShopAds) {
      list.push({ key: 'sa', label: 'Running SA', className: 'badge badge-muted' });
    }
    return list;
  }, [product]);

  if (!badges.length) {
    return null;
  }

  return (
    <div className="gmv-product-row__badges" aria-label="商品标签">
      {badges.map((badge) => (
        <span key={badge.key} className={badge.className}>
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
  const priceRange = useMemo(
    () => formatPriceRange(product.minPrice, product.maxPrice, product.currency),
    [product.minPrice, product.maxPrice, product.currency],
  );

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
        src={product.productImageUrl || PLACEHOLDER}
        alt={product.title || '商品缩略图'}
        className="thumb-56"
        loading="lazy"
        onError={(event) => {
          if (event.currentTarget?.dataset?.fallbackApplied) return;
          if (event.currentTarget) {
            event.currentTarget.dataset.fallbackApplied = 'true';
            event.currentTarget.src = PLACEHOLDER;
          }
        }}
      />
      <div className="gmv-product-row__content">
        <div className="gmv-product-row__header">
          <div className="gmv-product-row__title">{product.title || '未命名商品'}</div>
          <div className="gmv-product-row__price">{priceRange}</div>
        </div>
        {product.category ? (
          <div className="gmv-product-row__category">{product.category}</div>
        ) : null}
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
