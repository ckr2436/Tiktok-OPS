import { useMemo } from 'react';

import Loading from '@/components/ui/Loading.jsx';

import { formatMoney, getProductIdentifier, isProductAvailable } from './helpers.js';
import { GmvMaxTexts } from '../../locale.js';

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
  const allIds = useMemo(
    () => productRows.map((product) => getProductIdentifier(product)).filter(Boolean),
    [productRows],
  );
  const allSelected = allIds.length > 0 && allIds.every((id) => selection.has(id));

  if (loading) {
    return <Loading text="商品加载中…" />;
  }

  if (productRows.length === 0) {
    return <p>{emptyMessage || '暂无可用商品。'}</p>;
  }

  return (
    <div className="gmvmax-product-table">
      <div className="gmvmax-product-table__actions">
        <label>
          <input
            type="checkbox"
            checked={allSelected}
            onChange={() => onToggleAll?.(allIds)}
            disabled={disabled || allIds.length === 0}
          />
          <span>{GmvMaxTexts.selectAll}</span>
        </label>
        <span className="gmvmax-product-table__count">
          已选 {selection.size} / {productRows.length}
        </span>
        <div className="gmvmax-product-table__bulk-actions">
          <button
            type="button"
            onClick={() => onSelectAll?.(allIds)}
            disabled={disabled || allIds.length === 0}
          >
            全选店铺商品
          </button>
          <button type="button" onClick={() => onClearAll?.()} disabled={disabled || selection.size === 0}>
            取消全选
          </button>
        </div>
      </div>
      <table className="gmvmax-table">
        <thead>
          <tr>
            <th aria-label="select" />
            <th>商品</th>
            <th>商品 ID</th>
            <th>店铺</th>
            <th>GMV Max 状态</th>
            <th>可投放状态</th>
            <th>价格</th>
          </tr>
        </thead>
        <tbody>
          {productRows.map((product) => {
            const id = getProductIdentifier(product);
            if (!id) return null;
            const checked = selection.has(id);
            const imageUrl =
              product.image_url || product.cover_image || product.thumbnail_url || product.imageUrl || null;
            const storeKey = String(product.store_id ?? product.storeId ?? '');
            const storeLabel = storeKey && storeNames?.get(storeKey) ? storeNames.get(storeKey) : storeKey || '—';
            const gmvMaxStatus = product.gmv_max_ads_status || '—';
            const availability = isProductAvailable(product)
              ? GmvMaxTexts.availabilityAvailable
              : GmvMaxTexts.availabilityUnavailable;
            const price = product.price || product.sale_price || product.salePrice || product.gmv || null;
            return (
              <tr key={id}>
                <td>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => onToggle?.(id)}
                    disabled={disabled}
                    aria-label={`选择商品 ${id}`}
                  />
                </td>
                <td>
                  <div className="gmvmax-product-name">
                    <div className="gmvmax-product-thumb" aria-hidden="true">
                      {imageUrl ? (
                        <img src={imageUrl} alt="" loading="lazy" />
                      ) : (
                        <span className="gmvmax-product-thumb--empty">📦</span>
                      )}
                    </div>
                    <span>{product.title || product.name || product.product_name || product.productName || id}</span>
                  </div>
                </td>
                <td>{id}</td>
                <td>{storeLabel}</td>
                <td>{gmvMaxStatus}</td>
                <td>{availability}</td>
                <td>{price ? formatMoney(price) : '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
