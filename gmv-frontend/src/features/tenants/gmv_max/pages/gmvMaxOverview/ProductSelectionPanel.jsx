import { useMemo } from 'react';

import Loading from '@/components/ui/Loading.jsx';

import { getProductIdentifier, isProductAvailable } from './helpers.js';

export default function ProductSelectionPanel({
  products,
  selectedIds,
  onToggle,
  onToggleAll,
  storeNames,
  loading,
  emptyMessage,
  disabled,
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
    return <Loading text="Loading products…" />;
  }

  if (productRows.length === 0) {
    return <p>{emptyMessage || 'No products available.'}</p>;
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
          <span>Select all</span>
        </label>
        <span className="gmvmax-product-table__count">
          Selected {selection.size} / {productRows.length}
        </span>
      </div>
      <table className="gmvmax-table">
        <thead>
          <tr>
            <th aria-label="select" />
            <th>Product</th>
            <th>Product ID</th>
            <th>Store</th>
            <th>GMV Max status</th>
            <th>Availability</th>
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
            const availability = isProductAvailable(product) ? 'Available' : 'Not available';
            return (
              <tr key={id}>
                <td>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => onToggle?.(id)}
                    disabled={disabled}
                    aria-label={`Toggle product ${id}`}
                  />
                </td>
                <td>
                  <div className="gmvmax-product-name">
                    {imageUrl ? <img src={imageUrl} alt="" aria-hidden="true" /> : <span aria-hidden="true">📦</span>}
                    <span>{product.title || product.name || product.product_name || product.productName || id}</span>
                  </div>
                </td>
                <td>{id}</td>
                <td>{storeLabel}</td>
                <td>{gmvMaxStatus}</td>
                <td>{availability}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
