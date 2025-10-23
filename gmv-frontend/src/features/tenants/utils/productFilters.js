function isChangeType(value) {
  const type = String(value || '').toLowerCase()
  return ['added', 'add', 'create', 'created', 'removed', 'delete', 'deleted', 'updated', 'update'].some((token) =>
    type.includes(token)
  )
}

function filterProducts({ products = [], selectedShopIds = [], selectedAdvIds = [], selectedBCIds = [], filters = {} }) {
  let base = products
  if (selectedShopIds.length > 0) {
    base = base.filter((p) => selectedShopIds.includes(p.shopId))
  } else if (selectedAdvIds.length > 0) {
    base = base.filter((p) => selectedAdvIds.includes(p.advertiserId))
  } else if (selectedBCIds.length > 0) {
    base = base.filter((p) => selectedBCIds.includes(p.bcId))
  }
  if (filters.onlyChanges) {
    base = base.filter((p) => isChangeType(p.changeType) || isChangeType(p.status))
  }
  if (filters.onlyFailed) {
    base = base.filter((p) => String(p.status || '').toLowerCase().includes('fail'))
  }
  return base
}

export { filterProducts, isChangeType }
