const RAW_KEYS = {
  itemGroupId: ['item', 'group', 'id'].join('_'),
  productImageUrl: ['product', 'image', 'url'].join('_'),
  gmvMaxAdsStatus: ['gmv', 'max', 'ads', 'status'].join('_'),
  runningCustomShopAds: ['is', 'running', 'custom', 'shop', 'ads'].join('_'),
};

export function adaptProduct(item = {}) {
  if (!item || typeof item !== 'object') {
    return {
      itemGroupId: '',
      title: '',
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

  const product = {
    itemGroupId: item.itemGroupId ?? item[RAW_KEYS.itemGroupId] ?? '',
    title: item.title ?? item.product_title ?? '',
    imageUrl: item.imageUrl ?? item[RAW_KEYS.productImageUrl] ?? '',
    minPrice: item.minPrice ?? item.min_price ?? '',
    maxPrice: item.maxPrice ?? item.max_price ?? '',
    currency: item.currency ?? '',
    historicalSales: item.historicalSales ?? item.historical_sales ?? '',
    category: item.category ?? item.product_category ?? item.category_name ?? '',
    status: item.status ?? '',
    gmvMaxAdsStatus: item.gmvMaxAdsStatus ?? item[RAW_KEYS.gmvMaxAdsStatus] ?? '',
    isRunningCustomShopAds: Boolean(
      item.isRunningCustomShopAds ?? item[RAW_KEYS.runningCustomShopAds] ?? false,
    ),
    raw: item,
  };

  return product;
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
