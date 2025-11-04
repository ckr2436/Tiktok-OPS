const EMPTY_PRODUCT = {
  itemGroupId: '',
  title: '',
  imageUrl: '',
  productImageUrl: '',
  minPrice: '',
  maxPrice: '',
  currency: '',
  historicalSales: '',
  category: '',
  status: '',
  gmvMaxAdsStatus: '',
  isRunningCustomShopAds: false,
};

function ensureString(value) {
  if (value === undefined || value === null) {
    return '';
  }
  return String(value);
}

export function mapStoreProduct(api = {}) {
  if (!api || typeof api !== 'object') {
    return { ...EMPTY_PRODUCT };
  }

  const title =
    api.title
    ?? api.product_title
    ?? api.productName
    ?? api.product_name
    ?? '';

  const imageUrl =
    api.imageUrl
    ?? api.productImageUrl
    ?? api.product_image_url
    ?? api.cover
    ?? '';

  return {
    itemGroupId: ensureString(api.itemGroupId ?? api.item_group_id ?? ''),
    title: ensureString(title),
    imageUrl: ensureString(imageUrl),
    productImageUrl: ensureString(imageUrl),
    minPrice: ensureString(api.minPrice ?? api.min_price ?? ''),
    maxPrice: ensureString(api.maxPrice ?? api.max_price ?? ''),
    currency: ensureString(api.currency ?? ''),
    historicalSales: ensureString(api.historicalSales ?? api.historical_sales ?? ''),
    category: ensureString(api.category ?? api.product_category ?? api.category_name ?? ''),
    status: ensureString(api.status ?? ''),
    gmvMaxAdsStatus: ensureString(api.gmvMaxAdsStatus ?? api.gmv_max_ads_status ?? ''),
    isRunningCustomShopAds: Boolean(
      api.isRunningCustomShopAds ?? api.is_running_custom_shop_ads ?? false,
    ),
  };
}

export function mapPageInfo(info = {}) {
  const page = Number(info.page ?? info.page_no ?? info.current_page ?? 1);
  const pageSize = Number(info.pageSize ?? info.page_size ?? info.size ?? 50);
  const totalNumber = Number(
    info.totalNumber
      ?? info.total_number
      ?? info.total
      ?? info.total_items
      ?? info.total_count
      ?? 0,
  );
  const totalPage = Number(
    info.totalPage
      ?? info.total_page
      ?? (pageSize > 0 ? Math.ceil(totalNumber / pageSize) : 0),
  );

  return {
    page: Number.isNaN(page) ? 1 : page,
    pageSize: Number.isNaN(pageSize) ? 50 : pageSize,
    totalNumber: Number.isNaN(totalNumber) ? 0 : totalNumber,
    totalPage: Number.isNaN(totalPage) ? (pageSize > 0 ? Math.ceil(totalNumber / pageSize) : 0) : totalPage,
  };
}
