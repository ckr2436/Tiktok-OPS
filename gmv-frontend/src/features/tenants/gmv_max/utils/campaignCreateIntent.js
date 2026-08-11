const CAMPAIGN_CREATE_INTENT_STORAGE_PREFIX = 'gmvmax.campaign-create-intent.v1';
const MAX_SIGNED_64_BIT_INTEGER = '9223372036854775807';

export function buildCampaignCreateIntentKey({
  workspaceId,
  provider,
  authId,
  advertiserId,
  storeId,
  productId,
  launchMode,
}) {
  const scopeParts = [
    workspaceId,
    provider,
    authId,
    advertiserId,
    storeId,
    productId,
    launchMode,
  ].map((value) => encodeURIComponent(String(value ?? '')));
  return `${CAMPAIGN_CREATE_INTENT_STORAGE_PREFIX}:${scopeParts.join(':')}`;
}

function getPersistentCreateIntentStorage() {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage || null;
  } catch {
    return null;
  }
}

function isValidOfficialCreateRequestId(value) {
  const requestId = String(value || '');
  if (!/^[1-9]\d{0,18}$/.test(requestId)) return false;
  return requestId.length < MAX_SIGNED_64_BIT_INTEGER.length ||
    (requestId.length === MAX_SIGNED_64_BIT_INTEGER.length &&
      requestId <= MAX_SIGNED_64_BIT_INTEGER);
}

function generateOfficialCreateRequestId() {
  let randomValue = Math.floor(Math.random() * 1_000_000);
  try {
    const cryptoApi = typeof globalThis !== 'undefined' ? globalThis.crypto : null;
    if (cryptoApi?.getRandomValues) {
      const values = new Uint32Array(1);
      cryptoApi.getRandomValues(values);
      randomValue = values[0] % 1_000_000;
    }
  } catch {
    // Math.random is sufficient as a non-security fallback for an idempotency key.
  }
  return `${Date.now()}${String(randomValue).padStart(6, '0')}`;
}

function isValidCampaignCreateIntent(value) {
  return Boolean(
    value &&
    typeof value === 'object' &&
    String(value.campaign_name || '').trim() &&
    isValidOfficialCreateRequestId(value.request_id),
  );
}

export function getStoredCampaignCreateIntent({
  storageKey,
  fallbackIntents,
}) {
  const fallbackIntent = fallbackIntents.get(storageKey);
  if (isValidCampaignCreateIntent(fallbackIntent)) return fallbackIntent;

  const storage = getPersistentCreateIntentStorage();
  if (storage) {
    try {
      const stored = JSON.parse(storage.getItem(storageKey) || 'null');
      if (isValidCampaignCreateIntent(stored)) {
        fallbackIntents.set(storageKey, stored);
        return stored;
      }
      storage.removeItem(storageKey);
    } catch {
      try {
        storage.removeItem(storageKey);
      } catch {
        // Continue with the in-memory fallback when persistent storage is unavailable.
      }
    }
  }
  return null;
}

export function getOrCreateCampaignCreateIntent({
  storageKey,
  campaignName,
  fallbackIntents,
}) {
  const storedIntent = getStoredCampaignCreateIntent({
    storageKey,
    fallbackIntents,
  });
  if (storedIntent) return storedIntent;

  const intent = {
    campaign_name: campaignName,
    request_id: generateOfficialCreateRequestId(),
  };
  fallbackIntents.set(storageKey, intent);
  const storage = getPersistentCreateIntentStorage();
  if (storage) {
    try {
      storage.setItem(storageKey, JSON.stringify(intent));
    } catch {
      // The component-level fallback still keeps retries stable in this render.
    }
  }
  return intent;
}

export function getFinalizedCampaignCreatePayload(intent) {
  const payload = intent?.create_payload;
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  if (String(payload.request_id || '') !== String(intent.request_id || '')) return null;
  if (String(payload.idempotency_key || '') !== String(intent.request_id || '')) return null;
  if (String(payload.campaign_name || '') !== String(intent.campaign_name || '')) return null;
  return payload;
}

export function finalizeCampaignCreateIntent({
  storageKey,
  intent,
  createPayload,
  fallbackIntents,
}) {
  // Normalize before both persistence and the first request so a retry loaded
  // from JSON has the exact same fields (including omission of undefined).
  const normalizedCreatePayload = JSON.parse(JSON.stringify(createPayload));
  const finalizedIntent = {
    campaign_name: intent.campaign_name,
    request_id: intent.request_id,
    create_payload: normalizedCreatePayload,
  };
  fallbackIntents.set(storageKey, finalizedIntent);
  const storage = getPersistentCreateIntentStorage();
  if (storage) {
    try {
      storage.setItem(storageKey, JSON.stringify(finalizedIntent));
    } catch {
      // The component-level fallback still keeps retries stable in this render.
    }
  }
  return normalizedCreatePayload;
}

export function clearCampaignCreateIntent(storageKey, fallbackIntents) {
  fallbackIntents.delete(storageKey);
  const storage = getPersistentCreateIntentStorage();
  if (!storage) return;
  try {
    storage.removeItem(storageKey);
  } catch {
    // A successful create must not be changed into a UI failure by storage access.
  }
}

export function isDefinitiveCreateRejection(error) {
  const statusCode = Number(error?.response?.status);
  if (!Number.isFinite(statusCode) || statusCode < 400 || statusCode >= 500) {
    return false;
  }
  const detail = error?.response?.data?.detail;
  const code = String(detail?.details?.code || detail?.code || '').toUpperCase();
  return ![
    'GMVMAX_CREATE_OUTCOME_UNKNOWN',
    'GMVMAX_CREATE_PENDING_CONFIRMATION',
    'GMVMAX_MUTATION_INFLIGHT',
    'GMVMAX_MUTATION_FENCE_LOST',
  ].includes(code);
}
