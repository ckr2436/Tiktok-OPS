export function buildBusinessCenterOptions(payload, selectedBcId) {
  const list = Array.isArray(payload?.bcs) ? payload.bcs : [];
  if (selectedBcId && !list.some((item) => String(item?.bc_id || '') === String(selectedBcId))) {
    return [...list, { bc_id: selectedBcId, name: selectedBcId }];
  }
  return list;
}

export function buildAdvertiserOptions(payload, bcId, selectedAdvertiserId) {
  const allAdvertisers = Array.isArray(payload?.advertisers) ? payload.advertisers : [];
  if (!bcId) {
    return allAdvertisers;
  }
  const links = payload?.links?.bc_to_advertisers || {};
  const allowed = new Set((links[bcId] || []).map((item) => String(item)));
  const filtered = allAdvertisers.filter((item) => {
    if (!item?.advertiser_id) return false;
    const id = String(item.advertiser_id);
    if (allowed.size > 0) {
      return allowed.has(id);
    }
    return item.bc_id && String(item.bc_id) === String(bcId);
  });
  const selected = allAdvertisers.find((item) => String(item?.advertiser_id || '') === String(selectedAdvertiserId));
  if (selectedAdvertiserId && !filtered.some((item) => String(item?.advertiser_id || '') === String(selectedAdvertiserId))) {
    if (selected) {
      filtered.push(selected);
    } else {
      filtered.push({ advertiser_id: selectedAdvertiserId });
    }
  }
  return filtered
    .filter((item) => item?.advertiser_id)
    .sort((a, b) => {
      const labelA = (a.display_name || a.name || a.advertiser_id || '').toString();
      const labelB = (b.display_name || b.name || b.advertiser_id || '').toString();
      return labelA.localeCompare(labelB, 'zh-CN');
    });
}

export function buildStoreOptions(payload, advertiserId, selectedStoreId) {
  const allStores = Array.isArray(payload?.stores) ? payload.stores : [];
  if (!advertiserId) {
    return allStores;
  }
  const links = payload?.links?.advertiser_to_stores || {};
  const allowed = new Set((links[advertiserId] || []).map((item) => String(item)));
  const filtered = allStores.filter((item) => {
    if (!item?.store_id) return false;
    const id = String(item.store_id);
    const matchesLink = allowed.size > 0 ? allowed.has(id) : true;
    const matchesAdvertiser = item.advertiser_id
      ? String(item.advertiser_id) === String(advertiserId)
      : true;
    return matchesLink && matchesAdvertiser;
  });
  const selected = allStores.find((item) => String(item?.store_id || '') === String(selectedStoreId));
  if (selectedStoreId && !filtered.some((item) => String(item?.store_id || '') === String(selectedStoreId))) {
    if (selected) {
      filtered.push(selected);
    } else {
      filtered.push({ store_id: selectedStoreId, advertiser_id: advertiserId });
    }
  }
  return filtered
    .filter((item) => item?.store_id)
    .sort((a, b) => {
      const labelA = (a.name || a.store_id || '').toString();
      const labelB = (b.name || b.store_id || '').toString();
      return labelA.localeCompare(labelB, 'zh-CN');
    });
}
