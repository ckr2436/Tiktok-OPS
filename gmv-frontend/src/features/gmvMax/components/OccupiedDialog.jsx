import Modal from '../../../components/ui/Modal.jsx';

const PLACEHOLDER_OCCUPIED = [
  {
    id: 'GMV-PLAN-1001',
    campaignName: 'GMV Max 春季推广',
    budget: '¥8,000',
    bidStrategy: '智能出价',
    status: 'Running',
  },
  {
    id: 'GMV-PLAN-1012',
    campaignName: '618 爆品冲刺',
    budget: '¥12,000',
    bidStrategy: '手动出价（待接入）',
    status: 'Paused',
  },
];

export default function OccupiedDialog({ open = false, product = null, onClose }) {
  return (
    <Modal open={open} onClose={onClose} title="GMV Max 占用详情">
      <div className="occupied-dialog__content">
        <p className="occupied-dialog__hint">
          TODO：接入 /gmv_max/occupied_custom_shop_ads/list/ 接口，当前展示占位数据。
        </p>
        <div className="occupied-dialog__product">
          <strong>{product?.title || '未命名商品'}</strong>
          <span>ID：{product?.itemGroupId || '—'}</span>
        </div>
        <div className="occupied-dialog__list">
          {PLACEHOLDER_OCCUPIED.map((item) => (
            <div key={item.id} className="occupied-dialog__item">
              <div className="occupied-dialog__item-title">{item.campaignName}</div>
              <div className="occupied-dialog__meta">
                <span>计划 ID：{item.id}</span>
                <span>预算：{item.budget}</span>
                <span>出价策略：{item.bidStrategy}</span>
                <span>状态：{item.status}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </Modal>
  );
}

