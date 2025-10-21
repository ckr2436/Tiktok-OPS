// src/features/bc_ads_shop_product/statusUtils.js

export function formatStatusLabel(status) {
  const value = String(status || '').toLowerCase();
  if (value === 'success' || value === 'done' || value === 'completed') return '成功';
  if (value === 'failed' || value === 'error') return '失败';
  if (value === 'running' || value === 'pending' || value === 'processing' || value === 'scheduled') return '进行中';
  if (value === 'paused' || value === 'inactive' || value === 'disabled') return '已暂停';
  if (value === 'idle' || value === 'ready') return '就绪';
  return '未知';
}

export function statusTone(status) {
  const value = String(status || '').toLowerCase();
  if (value === 'success' || value === 'done' || value === 'completed') return 'ok';
  if (value === 'failed' || value === 'error') return 'danger';
  if (value === 'running' || value === 'pending' || value === 'processing' || value === 'scheduled') return 'warn';
  if (value === 'paused' || value === 'inactive' || value === 'disabled') return 'muted';
  if (value === 'idle' || value === 'ready') return 'ok';
  return 'muted';
}

export default { formatStatusLabel, statusTone };
