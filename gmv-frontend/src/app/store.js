import { configureStore } from '@reduxjs/toolkit';
import sessionReducer from '../features/platform/auth/sessionSlice.js';
import gmvMaxReducer, { rehydrate as rehydrateGmvMax } from '../features/tenants/gmv_max/state/gmvMaxSlice.js';
import { loadState as loadGmvMaxState, saveState as persistGmvMaxState } from '../features/tenants/gmv_max/state/persist.js';

const persistedGmvMax = typeof window !== 'undefined' ? loadGmvMaxState() : undefined;
const preloadedState = {};

if (persistedGmvMax) {
  preloadedState.gmvMax = gmvMaxReducer(undefined, rehydrateGmvMax(persistedGmvMax));
}

export const store = configureStore({
  reducer: {
    session: sessionReducer,
    gmvMax: gmvMaxReducer,
  },
  preloadedState,
  devTools: process.env.NODE_ENV !== 'production',
});

if (typeof window !== 'undefined') {
  store.subscribe(() => {
    const state = store.getState();
    persistGmvMaxState(state.gmvMax);
  });
}
