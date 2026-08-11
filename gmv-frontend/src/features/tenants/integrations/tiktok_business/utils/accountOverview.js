export function safeText(value, fallback = '-') {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  return text === '' ? fallback : text;
}

export function formatDateTime(value) {
  if (!value) return '-';
  try {
    return new Date(value).toLocaleString();
  } catch (err) {
    return safeText(value);
  }
}

export function extractBcName(row = {}) {
  return safeText(row.name ?? row.display_name);
}

export function extractAdvertiserName(row = {}) {
  return safeText(row.name ?? row.display_name);
}

export function extractShopName(row = {}) {
  const rawSource = row?.raw ?? row?.raw_json ?? {};
  const rawName = row.name ?? rawSource.store_name;
  return safeText(rawName);
}

export function deriveShopBcDisplay(row = {}) {
  const direct = row.bc_id;
  const raw = row?.raw ?? row?.raw_json ?? {};
  const fallbacks = [raw.store_authorized_bc_id, raw.authorized_bc_id, raw.bc_id];
  const fallback = fallbacks.find((item) => item && String(item).trim() !== '');
  const resolved = direct || fallback;
  return {
    value: resolved ? String(resolved) : '-',
    needsBackfill: !direct && Boolean(fallback),
  };
}

export function extractProductTitle(row = {}) {
  const raw = row?.raw ?? row?.raw_json ?? {};
  return safeText(row.title ?? raw.title ?? raw.product_title);
}

export function extractProductCurrency(row = {}) {
  const direct = row.currency;
  if (direct) return safeText(direct);
  const raw = row?.raw ?? row?.raw_json ?? {};
  const rawCurrency = raw.currency ?? raw?.price?.currency;
  return safeText(rawCurrency);
}

export function formatPrice(product = {}) {
  const { price, currency } = product || {};
  const rawSource = product?.raw ?? product?.raw_json ?? {};
  if (price === null || price === undefined) {
    const rawPrice = rawSource?.price;
    if (rawPrice && typeof rawPrice === 'object') {
      const amount = rawPrice.amount ?? rawPrice.value ?? rawPrice.price;
      const curr = rawPrice.currency ?? currency ?? rawSource.currency;
      if (amount !== undefined && amount !== null) {
        const num = Number(amount);
        const amountText = Number.isFinite(num) ? num.toFixed(2) : safeText(amount);
        return curr ? `${curr} ${amountText}` : amountText;
      }
    }
    return '-';
  }

  if (typeof price === 'number') {
    const formatted = Number.isFinite(price) ? price.toFixed(2) : safeText(price);
    return currency ? `${currency} ${formatted}` : formatted;
  }

  if (typeof price === 'object') {
    const amount = price.amount ?? price.value ?? price.price;
    const curr = price.currency ?? currency ?? rawSource.currency;
    if (amount !== undefined && amount !== null) {
      const num = Number(amount);
      const amountText = Number.isFinite(num) ? num.toFixed(2) : safeText(amount);
      return curr ? `${curr} ${amountText}` : amountText;
    }
  }

  const text = safeText(price);
  return currency ? `${currency} ${text}` : text;
}

export function formatStock(product = {}) {
  const { stock } = product || {};
  if (stock === null || stock === undefined) {
    const raw = product?.raw ?? product?.raw_json;
    if (raw && typeof raw === 'object') {
      const rawStock = raw.stock ?? raw.inventory ?? raw?.quantity;
      if (rawStock !== undefined && rawStock !== null) {
        if (typeof rawStock === 'object') {
          if ('available' in rawStock) return safeText(rawStock.available);
          if ('quantity' in rawStock) return safeText(rawStock.quantity);
        }
        return safeText(rawStock);
      }
    }
    return '-';
  }
  if (typeof stock === 'object') {
    if ('quantity' in stock) return safeText(stock.quantity);
    if ('available' in stock) return safeText(stock.available);
  }
  return safeText(stock);
}

export function mergeOptions(existing, incoming, getKey) {
  const map = new Map(existing.map((item) => [getKey(item), item]));
  incoming.forEach((row) => {
    const option = typeof row === 'object' && row !== null ? row : {};
    map.set(getKey(option), option);
  });
  return Array.from(map.values());
}
