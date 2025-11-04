const RAW_KEYS = {
  itemGroupId: ['item', 'group', 'id'].join('_'),
  productImageUrl: ['product', 'image', 'url'].join('_'),
  minPrice: ['min', 'price'].join('_'),
  maxPrice: ['max', 'price'].join('_'),
  historicalSales: ['historical', 'sales'].join('_'),
  gmvMaxAdsStatus: ['gmv', 'max', 'ads', 'status'].join('_'),
  runningCustomShopAds: ['is', 'running', 'custom', 'shop', 'ads'].join('_'),
};

function ensureString(value) {
  if (value === undefined || value === null) return '';
  return String(value);
}

export function adaptProduct(item = {}) {
  if (!item || typeof item !== 'object') {
    return {
      itemGroupId: '',
      title: '',
      productImageUrl: '',
      imageUrl: '',
      minPrice: '',
      maxPrice: '',
      currency: '',
      historicalSales: '',
      category: '',
      status: '',
      gmvMaxAdsStatus: '',
      isRunningCustomShopAds: false,
      raw: item,
    };
  }

  const itemGroupId = item.itemGroupId ?? item[RAW_KEYS.itemGroupId] ?? '';
  const productImageUrl =
    item.productImageUrl ?? item[RAW_KEYS.productImageUrl] ?? item.imageUrl ?? '';

  return {
    itemGroupId: ensureString(itemGroupId),
    title: ensureString(item.title ?? item.product_title ?? ''),
    productImageUrl: ensureString(productImageUrl),
    imageUrl: ensureString(productImageUrl),
    minPrice: ensureString(item.minPrice ?? item[RAW_KEYS.minPrice] ?? ''),
    maxPrice: ensureString(item.maxPrice ?? item[RAW_KEYS.maxPrice] ?? ''),
    currency: ensureString(item.currency ?? ''),
    historicalSales: ensureString(
      item.historicalSales ?? item[RAW_KEYS.historicalSales] ?? '',
    ),
    category: ensureString(
      item.category ?? item.product_category ?? item.category_name ?? '',
    ),
    status: ensureString(item.status ?? ''),
    gmvMaxAdsStatus: ensureString(
      item.gmvMaxAdsStatus ?? item[RAW_KEYS.gmvMaxAdsStatus] ?? '',
    ),
    isRunningCustomShopAds: Boolean(
      item.isRunningCustomShopAds ?? item[RAW_KEYS.runningCustomShopAds] ?? false,
    ),
    raw: item,
  };
}

export function adaptPageInfo(info = {}) {
  const page = Number(
    info.page
      ?? info.page_no
      ?? info.current_page
      ?? info.pageIndex
      ?? 1,
  );
  const pageSize = Number(
    info.page_size
      ?? info.pageSize
      ?? info.size
      ?? info.page_size ?? 0,
  ) || 10;
  const total = Number(
    info.total
      ?? info.total_number
      ?? info.totalNumber
      ?? info.total_count
      ?? info.totalCount
      ?? info.total_items
      ?? info.totalItems
      ?? 0,
  );
  const totalPages = pageSize > 0 ? Math.ceil(total / pageSize) : 0;
  return {
    page: Number.isNaN(page) ? 1 : page,
    pageSize: Number.isNaN(pageSize) ? 10 : pageSize,
    total: Number.isNaN(total) ? 0 : total,
    totalPages,
  };
}
