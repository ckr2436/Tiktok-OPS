import Modal from '../../../components/ui/Modal.jsx';

export default function OccupiedDialog({ open = false, product = null, onClose }) {
  return (
    <Modal open={open} onClose={onClose} title="该 SPU 当前被 GMV Max 计划占用">
      <div className="occupied-dialog__content">
        <div className="occupied-dialog__product">
          <strong>{product?.title || '未命名商品'}</strong>
          <span>SPU ID：{product?.itemGroupId || '—'}</span>
        </div>
        <div className="occupied-dialog__placeholder">
          <p>TODO：接入 /gmv_max/occupied_custom_shop_ads/list/ 接口。</p>
          <p>
            请求参数：occupied_asset_type=SPU、asset_id=
            {product?.itemGroupId || '—'}、advertiser_id={product?.raw?.advertiser_id || '—'}。
          </p>
          <p>当前暂以占位数据呈现，后续将展示占用计划列表。</p>
        </div>
      </div>
    </Modal>
  );
}

