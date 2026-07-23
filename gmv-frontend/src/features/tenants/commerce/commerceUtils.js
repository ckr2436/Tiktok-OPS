const moneyFormatters = new Map();

function moneyFormatter(currency) {
  const key = String(currency || 'USD').toUpperCase();
  if (!moneyFormatters.has(key)) {
    moneyFormatters.set(
      key,
      new Intl.NumberFormat('zh-CN', {
        style: 'currency',
        currency: key,
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }),
    );
  }
  return moneyFormatters.get(key);
}

function hasValue(value) {
  return value !== null && value !== undefined && value !== '';
}

export function formatMoney(value, currency = 'USD') {
  if (!hasValue(value)) return '--';
  const number = Number(value);
  return Number.isFinite(number) ? moneyFormatter(currency).format(number) : '--';
}

export function formatNumber(value) {
  if (!hasValue(value)) return '--';
  const number = Number(value);
  return Number.isFinite(number)
    ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(number)
    : '--';
}

export function formatRatio(value, digits = 2) {
  if (!hasValue(value)) return '--';
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : '--';
}

export function formatPercent(value, digits = 1) {
  if (!hasValue(value)) return '--';
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(digits)}%` : '--';
}

export function formatDateTime(value) {
  if (!value) return '尚未同步';
  const raw = String(value);
  const parsed = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(raw) ? raw : `${raw}Z`);
  if (Number.isNaN(parsed.getTime())) return raw;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(parsed);
}

export function dateInTimezone(value, timezone) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone || 'UTC',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(value);
  const values = Object.fromEntries(
    parts.filter((item) => item.type !== 'literal').map((item) => [item.type, item.value]),
  );
  return `${values.year}-${values.month}-${values.day}`;
}

function shiftDate(value, days) {
  const date = new Date(`${value}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

export function rangeForPreset(preset, timezone) {
  const today = dateInTimezone(new Date(), timezone);
  if (preset === 'yesterday') {
    const yesterday = shiftDate(today, -1);
    return { start_date: yesterday, end_date: yesterday };
  }
  if (preset === 'today') {
    return { start_date: today, end_date: today };
  }
  const days = preset === '30d' ? 30 : 7;
  return {
    start_date: shiftDate(today, -(days - 1)),
    end_date: today,
  };
}

export function rangeLabel(startDate, endDate) {
  if (!startDate || !endDate) return '';
  return startDate === endDate ? startDate : `${startDate} 至 ${endDate}`;
}

export function shortText(value, maxLength = 48) {
  const text = String(value || '').trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(1, maxLength - 1))}…`;
}

export function advertisersForShop(context, shopId) {
  const shops = Array.isArray(context?.shops) ? context.shops : [];
  const shop = shops.find((item) => String(item.id) === String(shopId));
  return Array.isArray(shop?.advertisers) ? shop.advertisers : [];
}

export function errorMessage(error) {
  return (
    error?.response?.data?.error?.message
    || error?.response?.data?.detail
    || error?.uiMessage
    || error?.message
    || '请求失败，请稍后重试。'
  );
}
