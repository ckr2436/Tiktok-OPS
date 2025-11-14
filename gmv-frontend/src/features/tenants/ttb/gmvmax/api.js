import http from "@/lib/http.js";

const DEFAULT_PROVIDER = "tiktok-business";

const normalizeProvider = (provider) =>
  (provider && String(provider).trim() ? String(provider).trim() : DEFAULT_PROVIDER)
    .toLowerCase()
    .replace(/_/g, "-");

const encodeSegment = (value) => encodeURIComponent(String(value));

const accountBase = (wid, provider, authId) =>
  `tenants/${encodeSegment(wid)}/providers/${encodeSegment(normalizeProvider(provider))}/accounts/${encodeSegment(authId)}`;

export const base = (wid, provider, authId) => `${accountBase(wid, provider, authId)}/gmvmax`;

const requireCampaignId = (campaignId) => {
  if (!campaignId) {
    throw new Error("campaignId is required to call this GMV Max API");
  }
  return campaignId;
};

const campaignPath = (wid, provider, authId, campaignId) =>
  `${base(wid, provider, authId)}/${requireCampaignId(campaignId)}`;

export const listCampaigns = (wid, provider, authId, params) =>
  http.get(base(wid, provider, authId), { params });

export const getCampaign = (wid, provider, authId, campaignId) =>
  http.get(campaignPath(wid, provider, authId, campaignId));

export const queryMetrics = (wid, provider, authId, campaignId, params) =>
  http.get(`${campaignPath(wid, provider, authId, campaignId)}/metrics`, { params });

export const syncMetrics = (wid, provider, authId, campaignId, body = {}) =>
  http.post(`${campaignPath(wid, provider, authId, campaignId)}/metrics/sync`, body);

export const listActionLogs = (wid, provider, authId, campaignId, params) =>
  http.get(`${campaignPath(wid, provider, authId, campaignId)}/actions`, { params });

export const applyAction = (wid, provider, authId, campaignId, body) =>
  http.post(`${campaignPath(wid, provider, authId, campaignId)}/actions`, body);

export const getStrategy = (wid, provider, authId, campaignId) =>
  http.get(`${campaignPath(wid, provider, authId, campaignId)}/strategy`);

export const updateStrategy = (wid, provider, authId, campaignId, patch) =>
  http.put(`${campaignPath(wid, provider, authId, campaignId)}/strategy`, patch);

export const previewStrategy = (wid, provider, authId, campaignId, body = {}) =>
  http.post(`${campaignPath(wid, provider, authId, campaignId)}/strategies/preview`, body);

export const syncCampaigns = (wid, provider, authId, body = { force: false }) =>
  http.post(`${base(wid, provider, authId)}/sync`, body);

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
