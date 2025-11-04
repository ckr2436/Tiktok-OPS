import { createSlice } from '@reduxjs/toolkit';

const initialState = {
  scope: {
    wid: '',
    authId: '',
    bcId: '',
    advertiserId: '',
    storeId: '',
  },
  ui: {
    filter: {
      keyword: '',
      onlyAvailable: false,
      onlyUnoccupied: false,
      eligibility: 'gmv_max',
    },
    sortBy: 'min_price',
    sortDir: 'asc',
  },
  productsByKey: {},
  fetchStateByKey: {},
};

function sanitizeScope(scope = {}) {
  if (!scope || typeof scope !== 'object') return { ...initialState.scope };
  return {
    wid: scope.wid || '',
    authId: scope.authId || '',
    bcId: scope.bcId || '',
    advertiserId: scope.advertiserId || '',
    storeId: scope.storeId || '',
  };
}

const gmvMaxSlice = createSlice({
  name: 'gmvMax',
  initialState,
  reducers: {
    setScope(state, action) {
      state.scope = { ...state.scope, ...sanitizeScope(action.payload) };
    },
    setFilter(state, action) {
      const payload = action.payload || {};
      state.ui.filter = {
        ...state.ui.filter,
        ...payload,
      };
    },
    setSort(state, action) {
      const { sortBy, sortDir } = action.payload || {};
      if (sortBy) state.ui.sortBy = sortBy;
      if (sortDir) state.ui.sortDir = sortDir;
    },
    upsertProducts(state, action) {
      const { key, payload } = action.payload || {};
      if (!key) return;
      const next = {
        items: Array.isArray(payload?.items) ? payload.items : [],
        total: payload?.total ?? 0,
        page: payload?.page ?? 1,
        pageSize: payload?.pageSize ?? payload?.page_size ?? 10,
        etag: payload?.etag || null,
        cachedAt: payload?.cachedAt || payload?.cached_at || new Date().toISOString(),
      };
      state.productsByKey[key] = next;
      if (state.fetchStateByKey[key]) {
        state.fetchStateByKey[key] = {
          ...state.fetchStateByKey[key],
          status: 'succeeded',
          error: null,
        };
      }
    },
    setFetchState(state, action) {
      const { key, status, error, requestId } = action.payload || {};
      if (!key) return;
      state.fetchStateByKey[key] = {
        ...(state.fetchStateByKey[key] || {}),
        status: status || state.fetchStateByKey[key]?.status || 'idle',
        error: error === undefined ? state.fetchStateByKey[key]?.error || null : error,
        requestId: requestId || state.fetchStateByKey[key]?.requestId || null,
      };
    },
    clearFetchState(state, action) {
      const { key } = action.payload || {};
      if (key && state.fetchStateByKey[key]) {
        delete state.fetchStateByKey[key];
      }
    },
    rehydrate(_, action) {
      const payload = action.payload;
      if (!payload || typeof payload !== 'object') {
        return initialState;
      }
      const scope = sanitizeScope(payload.scope);
      const ui = {
        ...initialState.ui,
        ...(payload.ui || {}),
        filter: {
          ...initialState.ui.filter,
          ...(payload.ui?.filter || {}),
        },
      };
      const productsByKey = {};
      if (payload.productsByKey && typeof payload.productsByKey === 'object') {
        Object.entries(payload.productsByKey).forEach(([key, value]) => {
          if (!value || typeof value !== 'object') return;
          productsByKey[key] = {
            items: Array.isArray(value.items) ? value.items : [],
            total: value.total ?? 0,
            page: value.page ?? 1,
            pageSize: value.pageSize ?? value.page_size ?? 10,
            etag: value.etag || null,
            cachedAt: value.cachedAt || value.cached_at || null,
          };
        });
      }
      const fetchStateByKey = {};
      if (payload.fetchStateByKey && typeof payload.fetchStateByKey === 'object') {
        Object.entries(payload.fetchStateByKey).forEach(([key, value]) => {
          if (!value || typeof value !== 'object') return;
          fetchStateByKey[key] = {
            status: value.status || 'idle',
            error: value.error || null,
            requestId: null,
          };
        });
      }
      return {
        scope,
        ui,
        productsByKey,
        fetchStateByKey,
      };
    },
  },
});

export const {
  setScope,
  setFilter,
  setSort,
  upsertProducts,
  setFetchState,
  clearFetchState,
  rehydrate,
} = gmvMaxSlice.actions;

export default gmvMaxSlice.reducer;

export const selectGmvMaxScope = (state) => state.gmvMax.scope;
export const selectGmvMaxUI = (state) => state.gmvMax.ui;
export const selectProductsByKey = (state) => state.gmvMax.productsByKey;
export const selectFetchStateByKey = (state) => state.gmvMax.fetchStateByKey;
