import http from "@/lib/http.js";

const prefix = (wid, authId) => `/api/v1/tenants/${wid}/ttb/accounts/${authId}/gmvmax`;

export const listCampaigns = (wid, authId, params) =>
  http.get(`${prefix(wid, authId)}/campaigns`, { params });

export const getCampaign = (wid, authId, campaignId) =>
  http.get(`${prefix(wid, authId)}/campaigns/${campaignId}`);

export const syncCampaigns = (wid, authId, body = { force: false }) =>
  http.post(`${prefix(wid, authId)}/campaigns/sync`, body);

export const syncMetrics = (wid, authId, body) =>
  http.post(`${prefix(wid, authId)}/campaigns/metrics/sync`, body);

export const queryMetrics = (wid, authId, params) =>
  http.get(`${prefix(wid, authId)}/campaigns/metrics`, { params });

export const listActionLogs = (wid, authId, params) =>
  http.get(`${prefix(wid, authId)}/campaigns/actions`, { params });

export const applyAction = (wid, authId, body) =>
  http.post(`${prefix(wid, authId)}/campaigns/actions`, body);

export const getStrategy = (wid, authId, campaignId) =>
  http.get(`${prefix(wid, authId)}/campaigns/${campaignId}/strategy`);

export const updateStrategy = (wid, authId, campaignId, patch) =>
  http.put(`${prefix(wid, authId)}/campaigns/${campaignId}/strategy`, patch);

export const previewStrategy = (wid, authId, campaignId, body = {}) =>
  http.post(`${prefix(wid, authId)}/campaigns/${campaignId}/strategy/preview`, body);

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
