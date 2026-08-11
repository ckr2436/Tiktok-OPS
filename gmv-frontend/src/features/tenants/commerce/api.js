import http from '@/lib/http.js';

function prefix(workspaceId) {
  return `/tenants/${encodeURIComponent(workspaceId)}/commerce`;
}

function payload(response) {
  return response?.data ?? response;
}

export async function getCommerceContext(workspaceId, config = {}) {
  return payload(await http.get(`${prefix(workspaceId)}/context`, config));
}

export async function getCommerceOverview(workspaceId, params = {}, config = {}) {
  return payload(
    await http.get(`${prefix(workspaceId)}/overview`, {
      ...config,
      params,
    }),
  );
}

export async function getCommerceOrderSummary(workspaceId, params = {}, config = {}) {
  return payload(
    await http.get(`${prefix(workspaceId)}/orders/summary`, {
      ...config,
      params,
    }),
  );
}

export async function getCommerceSyncStatus(workspaceId, params = {}, config = {}) {
  return payload(
    await http.get(`${prefix(workspaceId)}/sync/status`, {
      ...config,
      params,
    }),
  );
}

export async function getProductCostHistory(
  workspaceId,
  productId,
  params = {},
  config = {},
) {
  return payload(
    await http.get(
      `${prefix(workspaceId)}/products/${encodeURIComponent(productId)}/costs`,
      { ...config, params },
    ),
  );
}

export async function saveProductCost(workspaceId, productId, values) {
  return payload(
    await http.put(
      `${prefix(workspaceId)}/products/${encodeURIComponent(productId)}/costs`,
      values,
    ),
  );
}

export async function getFlashSalePolicies(workspaceId, params = {}, config = {}) {
  return payload(
    await http.get(`${prefix(workspaceId)}/flash-sales`, {
      ...config,
      params,
    }),
  );
}

export async function applyFlashSalePlan(workspaceId, values) {
  return payload(
    await http.post(`${prefix(workspaceId)}/flash-sales/apply`, values),
  );
}

export async function reconcileFlashSales(workspaceId, shopId) {
  return payload(
    await http.post(`${prefix(workspaceId)}/flash-sales/reconcile`, null, {
      params: { shop_id: shopId },
    }),
  );
}

export async function syncCommerceData(workspaceId, values) {
  return payload(await http.post(`${prefix(workspaceId)}/sync`, values));
}
