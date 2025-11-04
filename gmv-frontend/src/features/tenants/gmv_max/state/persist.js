const STORAGE_KEY = 'gmv.max.slice';

function safeParse(value) {
  if (!value) return null;
  try {
    return JSON.parse(value);
  } catch (error) {
    console.warn('[gmvMax] Failed to parse persisted state', error);
    return null;
  }
}

export function loadState() {
  if (typeof window === 'undefined' || !window.localStorage) return undefined;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return safeParse(raw) || undefined;
  } catch (error) {
    console.warn('[gmvMax] Failed to load persisted state', error);
    return undefined;
  }
}

let saveTimer = null;
export function saveState(state) {
  if (typeof window === 'undefined' || !window.localStorage) return;
  try {
    if (saveTimer) {
      clearTimeout(saveTimer);
    }
    saveTimer = setTimeout(() => {
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
      } catch (error) {
        console.warn('[gmvMax] Failed to save persisted state', error);
      }
    }, 200);
  } catch (error) {
    console.warn('[gmvMax] Failed to schedule save', error);
  }
}

export function clearState() {
  if (typeof window === 'undefined' || !window.localStorage) return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch (error) {
    console.warn('[gmvMax] Failed to clear persisted state', error);
  }
}

export default {
  loadState,
  saveState,
  clearState,
};
