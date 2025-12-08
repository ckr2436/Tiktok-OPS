export const GmvMaxMetricsLevel = Object.freeze({
  OVERVIEW: "overview",
  CAMPAIGN: "campaign",
  PRODUCT: "product",
  CREATIVE: "creative",
});

export const GMV_MAX_LEVELS_REQUIRING_CAMPAIGN = new Set([
  GmvMaxMetricsLevel.PRODUCT,
  GmvMaxMetricsLevel.CREATIVE,
]);

export const GMV_MAX_LEVELS_REQUIRING_ITEM_GROUP = new Set([
  GmvMaxMetricsLevel.CREATIVE,
]);
