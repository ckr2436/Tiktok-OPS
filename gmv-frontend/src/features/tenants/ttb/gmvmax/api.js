import http from "@/lib/http.js";

const prefix = (wid, authId) => `/api/v1/tenants/${wid}/ttb/accounts/${authId}/gmvmax`;

const requireCampaignId = (campaignId) => {
  if (!campaignId) {
    throw new Error("campaignId is required to call this GMV Max API");
  }
  return campaignId;
};

export const listCampaigns = (wid, authId, params) =>
  http.get(`${prefix(wid, authId)}`, { params });

export const getCampaign = (wid, authId, campaignId) =>
  http.get(`${prefix(wid, authId)}/${requireCampaignId(campaignId)}`);

export const queryMetrics = (wid, authId, campaignId, params) =>
  http.get(`${prefix(wid, authId)}/${requireCampaignId(campaignId)}/metrics`, { params });

export const syncMetrics = (wid, authId, campaignId, body = {}) =>
  http.post(`${prefix(wid, authId)}/${requireCampaignId(campaignId)}/metrics/sync`, body);

export const applyAction = (wid, authId, body) =>
  http.post(`${prefix(wid, authId)}/campaigns/actions`, body);

export const listActionLogs = (wid, authId, params) =>
  http.get(`${prefix(wid, authId)}/campaigns/actions`, { params });

export const getStrategy = (wid, authId, campaignId) =>
  http.get(`${prefix(wid, authId)}/${requireCampaignId(campaignId)}/strategy`);

export const updateStrategy = (wid, authId, campaignId, patch) =>
  http.put(`${prefix(wid, authId)}/${requireCampaignId(campaignId)}/strategy`, patch);

export const previewStrategy = (wid, authId, campaignId, body = {}) =>
  http.post(`${prefix(wid, authId)}/${requireCampaignId(campaignId)}/strategy/preview`, body);

export const syncCampaigns = (wid, authId, body = { force: false }) =>
  http.post(`${prefix(wid, authId)}/campaigns/sync`, body);

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
