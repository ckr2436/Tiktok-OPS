import { useMemo } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { useSelector } from 'react-redux';
import { selectProductsByKey } from '../state/gmvMaxSlice.js';

function flattenProduct(product) {
  if (!product || typeof product !== 'object') return [];
  const entries = Object.entries(product).map(([key, value]) => {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      return [key, JSON.stringify(value, null, 2)];
    }
    if (Array.isArray(value)) {
      return [key, JSON.stringify(value, null, 2)];
    }
    return [key, value];
  });
  return entries.sort(([a], [b]) => a.localeCompare(b));
}

export default function ProductDetail() {
  const { item_group_id: itemGroupIdParam, wid } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const productsByKey = useSelector(selectProductsByKey);

  const fallbackProduct = useMemo(() => {
    if (!itemGroupIdParam) return null;
    const target = String(itemGroupIdParam);
    for (const cache of Object.values(productsByKey || {})) {
      if (!cache?.items) continue;
      const found = cache.items.find((item) => {
        const candidates = [item?.item_group_id, item?.product_id, item?.id];
        return candidates.some((candidate) => candidate && String(candidate) === target);
      });
      if (found) return found;
    }
    return null;
  }, [productsByKey, itemGroupIdParam]);

  const product = location.state?.product || fallbackProduct;

  const rows = useMemo(() => flattenProduct(product), [product]);

  return (
    <div className="gmv-product-detail page-container">
      <div className="page-header">
        <div>
          <h1>商品详情</h1>
          <p className="small-muted">GMV Max 商品的原始字段信息。</p>
        </div>
        <div className="page-header__actions">
          <button
            type="button"
            className="btn ghost"
            onClick={() => navigate(`/tenants/${wid}/gmv-max`, { replace: false })}
          >
            返回列表
          </button>
        </div>
      </div>

      {!product ? (
        <div className="card">
          <div className="empty-state">未找到该商品的缓存，请返回列表重新加载。</div>
        </div>
      ) : (
        <div className="card" style={{ overflowX: 'auto' }}>
          <table className="detail-table">
            <thead>
              <tr>
                <th>字段</th>
                <th>值</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([key, value]) => (
                <tr key={key}>
                  <td>{key}</td>
                  <td>
                    {typeof value === 'string' ? (
                      <pre>{value}</pre>
                    ) : (
                      String(value ?? '')
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
