import http from "@/lib/http.js";

const DEFAULT_PROVIDER = "tiktok-business";

const providerSegment = (provider = DEFAULT_PROVIDER) =>
  encodeURIComponent(provider || DEFAULT_PROVIDER);

export const base = (wid, authId, provider = DEFAULT_PROVIDER) =>
  `tenants/${wid}/providers/${providerSegment(provider)}/accounts/${authId}/gmvmax`;

const requireCampaignId = (campaignId) => {
  if (!campaignId) {
    throw new Error("campaignId is required to call this GMV Max API");
  }
  return campaignId;
};

const campaignPath = (wid, authId, campaignId) =>
  `${base(wid, authId)}/${requireCampaignId(campaignId)}`;

export const listCampaigns = (wid, authId, params) =>
  http.get(base(wid, authId), { params });

export const getCampaign = (wid, authId, campaignId) =>
  http.get(campaignPath(wid, authId, campaignId));

export const queryMetrics = (wid, authId, campaignId, params) =>
  http.get(`${campaignPath(wid, authId, campaignId)}/metrics`, { params });

export const syncMetrics = (wid, authId, campaignId, body = {}) =>
  http.post(`${campaignPath(wid, authId, campaignId)}/metrics/sync`, body);

export const listActionLogs = (wid, authId, campaignId, params) =>
  http.get(`${campaignPath(wid, authId, campaignId)}/actions`, { params });

export const applyAction = (wid, authId, campaignId, body) =>
  http.post(`${campaignPath(wid, authId, campaignId)}/actions`, body);

export const getStrategy = (wid, authId, campaignId) =>
  http.get(`${campaignPath(wid, authId, campaignId)}/strategy`);

export const updateStrategy = (wid, authId, campaignId, patch) =>
  http.put(`${campaignPath(wid, authId, campaignId)}/strategy`, patch);

export const previewStrategy = (wid, authId, campaignId, body = {}) =>
  http.post(`${campaignPath(wid, authId, campaignId)}/strategies/preview`, body);

export const syncCampaigns = (wid, authId, body = { force: false }) =>
  http.post(`${base(wid, authId)}/sync`, body);

export default {
  listCampaigns,
  getCampaign,
  syncCampaigns,
  syncMetrics,
  queryMetrics,
  listActionLogs,
  applyAction,
  getStrategy,
  updateStrategy,
  previewStrategy,
};
