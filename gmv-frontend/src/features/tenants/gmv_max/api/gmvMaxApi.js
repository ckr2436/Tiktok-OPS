import http from '@/lib/http.js';
import { normProvider } from '@/features/tenants/integrations/tiktok_business/service.js';

function encode(value) {
  return encodeURIComponent(value);
}

function tenantPrefix(workspaceId) {
  return `/tenants/${encode(workspaceId)}`;
}

function providerPrefix(workspaceId, provider) {
  return `${tenantPrefix(workspaceId)}/providers/${encode(normProvider(provider))}`;
}

function accountPrefix(workspaceId, provider, authId) {
  return `${providerPrefix(workspaceId, provider)}/accounts/${encode(authId)}`;
}

function mergeConfig(config = {}, params) {
  if (!params || (typeof params === 'object' && Object.keys(params).length === 0)) {
    return { ...config };
  }
  return {
    ...config,
    params: {
      ...(config.params || {}),
      ...params,
    },
  };
}

async function get(url, config) {
  const response = await http.get(url, config);
  return response.data;
}

async function post(url, body, config) {
  const response = await http.post(url, body, config);
  return response.data;
}

async function put(url, body, config) {
  const response = await http.put(url, body, config);
  return response.data;
}

export async function listProviders(workspaceId, config) {
  return get(`${tenantPrefix(workspaceId)}/providers`, config);
}

export async function listAccounts(workspaceId, provider, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${providerPrefix(workspaceId, provider)}/accounts`, axiosConfig);
}

export async function listBusinessCenters(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/business-centers`, axiosConfig);
}

export async function listAdvertisers(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/advertisers`, axiosConfig);
}

export async function listStores(workspaceId, provider, authId, options = {}, config) {
  const { advertiserId, ...params } = options || {};
  const axiosConfig = mergeConfig(config, params);
  const base = advertiserId
    ? `${accountPrefix(workspaceId, provider, authId)}/advertisers/${encode(advertiserId)}/stores`
    : `${accountPrefix(workspaceId, provider, authId)}/stores`;
  return get(base, axiosConfig);
}

export async function listProducts(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/products`, axiosConfig);
}

export async function getGmvMaxOptions(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/options`, axiosConfig);
}

export async function getGmvMaxConfig(workspaceId, provider, authId, config) {
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/config`, config);
}

export async function updateGmvMaxConfig(workspaceId, provider, authId, payload, config) {
  return put(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/config`, payload, config);
}

export async function syncGmvMaxCampaigns(workspaceId, provider, authId, payload, config) {
  return post(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/sync`, payload, config);
}

export async function listGmvMaxCampaigns(workspaceId, provider, authId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax`, axiosConfig);
}

export async function createGmvMaxCampaign(workspaceId, provider, authId, payload, config) {
  return post(`${accountPrefix(workspaceId, provider, authId)}/gmvmax`, payload, config);
}

export async function getGmvMaxCampaign(workspaceId, provider, authId, campaignId, config) {
  return get(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}`, config);
}

export async function updateGmvMaxCampaign(workspaceId, provider, authId, campaignId, payload, config) {
  return put(`${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}`, payload, config);
}

export async function syncGmvMaxMetrics(workspaceId, provider, authId, campaignId, payload, config) {
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/metrics/sync`,
    payload,
    config,
  );
}

export async function getGmvMaxMetrics(workspaceId, provider, authId, campaignId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/metrics`,
    axiosConfig,
  );
}

export async function applyGmvMaxAction(workspaceId, provider, authId, campaignId, payload, config) {
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/actions`,
    payload,
    config,
  );
}

export async function listGmvMaxActionLogs(workspaceId, provider, authId, campaignId, params, config) {
  const axiosConfig = mergeConfig(config, params);
  return get(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/actions`,
    axiosConfig,
  );
}

export async function getGmvMaxStrategy(workspaceId, provider, authId, campaignId, config) {
  return get(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/strategy`,
    config,
  );
}

export async function updateGmvMaxStrategy(workspaceId, provider, authId, campaignId, payload, config) {
  return put(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/strategy`,
    payload,
    config,
  );
}

export async function previewGmvMaxStrategy(workspaceId, provider, authId, campaignId, payload, config) {
  return post(
    `${accountPrefix(workspaceId, provider, authId)}/gmvmax/${encode(campaignId)}/strategies/preview`,
    payload,
    config,
  );
}
