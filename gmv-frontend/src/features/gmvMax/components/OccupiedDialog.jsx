import Modal from '../../../components/ui/Modal.jsx';

export default function OccupiedDialog({ open = false, product = null, onClose }) {
  return (
    <Modal open={open} onClose={onClose} title="该 SPU 当前被 GMV Max 计划占用">
      <div className="occupied-dialog__content">
        <div className="occupied-dialog__product">
          <strong>{product?.title || '未命名商品'}</strong>
          <span>SPU ID：{product?.itemGroupId || '—'}</span>
        </div>
        <div className="occupied-dialog__list" aria-live="polite">
          <div className="occupied-dialog__row">
            <span>计划名称</span>
            <span>计划 ID</span>
            <span>状态</span>
            <span>近 7 日 GMV</span>
          </div>
          <div className="occupied-dialog__row">
            <span className="skeleton-line" style={{ width: '80%' }} />
            <span className="skeleton-line" style={{ width: '60%' }} />
            <span className="skeleton-line" style={{ width: '50%' }} />
            <span className="skeleton-line" style={{ width: '70%' }} />
          </div>
          <div className="occupied-dialog__row">
            <span className="skeleton-line" style={{ width: '75%' }} />
            <span className="skeleton-line" style={{ width: '55%' }} />
            <span className="skeleton-line" style={{ width: '45%' }} />
            <span className="skeleton-line" style={{ width: '65%' }} />
          </div>
        </div>
        <div className="occupied-dialog__hint">
          TODO：接入 /gmv_max/occupied_custom_shop_ads/list/（occupied_asset_type=SPU，asset_id=
          {product?.itemGroupId || '—'}，advertiser_id={product?.raw?.advertiser_id || '—'}）。
        </div>
      </div>
    </Modal>
  );
}

