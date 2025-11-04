import { memo, useMemo } from 'react';

function formatCurrencyRange(product) {
  const currency = product?.currency || product?.price_currency || product?.min_price_currency;
  const min = product?.min_price ?? product?.price_min ?? product?.price_range?.min;
  const max = product?.max_price ?? product?.price_max ?? product?.price_range?.max;
  if (min === undefined && max === undefined) return null;
  const format = (value) => {
    if (value === undefined || value === null) return null;
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric.toLocaleString();
    return String(value);
  };
  const parts = [format(min), format(max)].filter(Boolean);
  if (!parts.length) return null;
  const rangeText = parts.length === 2 ? `${parts[0]} ~ ${parts[1]}` : parts[0];
  return currency ? `${rangeText} ${currency}` : rangeText;
}

function formatUpdatedTime(value) {
  if (!value) return null;
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', { hour12: false });
  } catch (error) {
    return typeof value === 'string' ? value : null;
  }
}

function normalizeTitle(product) {
  if (!product) return '';
  return (
    (typeof product.title === 'string' && product.title.trim())
    || (typeof product.name === 'string' && product.name.trim())
    || (product.raw?.title ? String(product.raw.title).trim() : '')
    || (product.item_group_id ? String(product.item_group_id) : '')
    || (product.product_id ? String(product.product_id) : '')
    || ''
  );
}

function Badge({ variant = 'default', children }) {
  const className = `gmv-product-card__badge gmv-product-card__badge--${variant}`;
  return <span className={className}>{children}</span>;
}

const ProductCard = memo(({ product, onClick }) => {
  const title = normalizeTitle(product) || '未命名商品';
  const status = String(product?.status || '').toUpperCase();
  const occupancy = String(product?.gmv_max_ads_status || '').toUpperCase();
  const skuCount = Number.isFinite(Number(product?.sku_count)) ? Number(product.sku_count) : null;
  const priceRange = formatCurrencyRange(product);
  const updatedTime = formatUpdatedTime(product?.updated_time || product?.ext_updated_time);
  const imageUrl = product?.product_image_url || product?.image_url || product?.cover_url;
  const itemGroupId = product?.item_group_id || product?.product_id || product?.id;

  const availabilityBadge = useMemo(() => {
    if (!status) return null;
    const variant = status === 'AVAILABLE' ? 'success' : 'warning';
    const label = status === 'AVAILABLE' ? '可投放' : status;
    return <Badge variant={variant}>{label}</Badge>;
  }, [status]);

  const occupancyBadge = useMemo(() => {
    if (!occupancy) return null;
    const variant = occupancy === 'UNOCCUPIED' ? 'info' : 'danger';
    const label = occupancy === 'UNOCCUPIED' ? '未占用' : '已占用';
    return <Badge variant={variant}>{label}</Badge>;
  }, [occupancy]);

  return (
    <button type="button" className="gmv-product-card" onClick={() => onClick?.(product)}>
      <div className="gmv-product-card__image">
        {imageUrl ? (
          <img src={imageUrl} alt={title} loading="lazy" />
        ) : (
          <div className="gmv-product-card__placeholder">无封面</div>
        )}
      </div>
      <div className="gmv-product-card__content">
        <div className="gmv-product-card__title" title={title}>{title}</div>
        <div className="gmv-product-card__badges">
          {availabilityBadge}
          {occupancyBadge}
        </div>
        <div className="gmv-product-card__meta">
          <span>SKU：{skuCount ?? '-'}</span>
          <span>价格：{priceRange ?? '未知'}</span>
        </div>
        <div className="gmv-product-card__footer">
          <span>ID：{itemGroupId || '-'}</span>
          <span>更新时间：{updatedTime || '未知'}</span>
        </div>
      </div>
    </button>
  );
});

ProductCard.displayName = 'ProductCard';

export default ProductCard;
