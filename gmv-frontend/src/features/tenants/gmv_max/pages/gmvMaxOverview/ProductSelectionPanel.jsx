import { useMemo } from 'react';

import Loading from '@/components/ui/Loading.jsx';

import { formatMoney, getProductIdentifier, isProductAvailable } from './helpers.js';

function getProductName(product, fallback) {
  return (
    product?.title ||
    product?.name ||
    product?.product_name ||
    product?.productName ||
    product?.item_name ||
    product?.itemName ||
    fallback
  );
}

function getProductImage(product) {
  return (
    product?.image_url ||
    product?.product_image_url ||
    product?.cover_image ||
    product?.thumbnail_url ||
    product?.imageUrl ||
    product?.coverImage ||
    product?.main_image ||
    null
  );
}

function getProductPrice(product) {
  return (
    product?.effective_price ||
    product?.effectivePrice ||
    product?.sale_price ||
    product?.salePrice ||
    product?.min_price ||
    product?.minPrice ||
    product?.price ||
    null
  );
}

function shortId(value) {
  const text = String(value || '');
  if (text.length <= 14) return text;
  return `${text.slice(0, 7)}...${text.slice(-5)}`;
}

function truncateName(value) {
  const text = String(value || '');
  if (text.length <= 58) return text;
  return `${text.slice(0, 58)}...`;
}

export default function ProductSelectionPanel({
  products,
  selectedIds,
  onToggle,
  onToggleAll,
  storeNames,
  loading,
  emptyMessage,
  disabled,
  onSelectAll,
  onClearAll,
  searchTerm = '',
  onSearchChange,
}) {
  const selection = useMemo(() => {
    if (selectedIds instanceof Set) return selectedIds;
    if (Array.isArray(selectedIds)) return new Set(selectedIds.map(String));
    return new Set();
  }, [selectedIds]);

  const productRows = useMemo(() => {
    if (!Array.isArray(products)) return [];
    return products.filter((product) => isProductAvailable(product));
  }, [products]);

  const filteredProducts = useMemo(() => {
    const query = String(searchTerm || '').trim().toLowerCase();
    if (!query) return productRows;
    return productRows.filter((product) => {
      const id = getProductIdentifier(product);
      const name = getProductName(product, '');
      return (
        (id && String(id).toLowerCase().includes(query)) ||
        (typeof name === 'string' && name.toLowerCase().includes(query))
      );
    });
  }, [productRows, searchTerm]);

  const allIds = useMemo(
    () => filteredProducts.map((product) => getProductIdentifier(product)).filter(Boolean),
    [filteredProducts],
  );
  const allSelected = allIds.length > 0 && allIds.every((id) => selection.has(String(id)));

  if (loading) {
    return <Loading text="商品加载中..." />;
  }

  if (filteredProducts.length === 0) {
    return <p className="gmvmax-placeholder">{emptyMessage || '暂无可投放商品'}</p>;
  }

  return (
    <div className="gmvmax-product-picker">
      <div className="gmvmax-product-picker__toolbar">
        <div className="gmvmax-product-table__search">
          <input
            type="search"
            value={searchTerm}
            onChange={(event) => onSearchChange?.(event.target.value)}
            placeholder="搜索商品名称或 ID"
          />
        </div>
        <label className="gmvmax-check-inline">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={() => onToggleAll?.(allIds)}
            disabled={disabled || allIds.length === 0}
          />
          <span>全选当前结果</span>
        </label>
        <span className="gmvmax-product-table__count">
          已选 {selection.size} / {filteredProducts.length}
        </span>
        <div className="gmvmax-product-table__bulk-actions">
          <button
            type="button"
            className="gmvmax-button gmvmax-button--secondary"
            onClick={() => onSelectAll?.(allIds)}
            disabled={disabled || allIds.length === 0}
          >
            全选店铺商品
          </button>
          <button
            type="button"
            className="gmvmax-button gmvmax-button--ghost"
            onClick={() => onClearAll?.()}
            disabled={disabled || selection.size === 0}
          >
            取消全选
          </button>
        </div>
      </div>

      <div className="gmvmax-product-grid">
        {filteredProducts.map((product) => {
          const id = getProductIdentifier(product);
          if (!id) return null;
          const idKey = String(id);
          const checked = selection.has(idKey);
          const imageUrl = getProductImage(product);
          const storeKey = String(product.store_id ?? product.storeId ?? '');
          const storeLabel = storeKey && storeNames?.get(storeKey) ? storeNames.get(storeKey) : storeKey || '-';
          const gmvMaxStatus = product.gmv_max_ads_status || product.gmvMaxAdsStatus || '-';
          const availability = isProductAvailable(product) ? '可投放' : '不可投放';
          const price = getProductPrice(product);
          const name = getProductName(product, idKey);
          return (
            <label
              key={idKey}
              className={`gmvmax-product-card ${checked ? 'gmvmax-product-card--selected' : ''}`}
              title={name}
            >
              <input
                type="checkbox"
                checked={checked}
                onChange={() => onToggle?.(idKey)}
                disabled={disabled}
                aria-label={`选择商品 ${idKey}`}
              />
              <div className="gmvmax-product-card__image" aria-hidden="true">
                {imageUrl ? <img src={imageUrl} alt="" loading="lazy" /> : <span>封面</span>}
              </div>
              <div className="gmvmax-product-card__body">
                <strong>{truncateName(name)}</strong>
                <span>ID {shortId(idKey)}</span>
                <span>{storeLabel}</span>
              </div>
              <div className="gmvmax-product-card__meta">
                <span>{gmvMaxStatus}</span>
                <span>{availability}</span>
                <b>{price ? formatMoney(price) : '-'}</b>
              </div>
            </label>
          );
        })}
      </div>
    </div>
  );
}
