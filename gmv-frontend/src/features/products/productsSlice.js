import { createSlice } from '@reduxjs/toolkit';

const initialState = {
  byKey: {},
  lists: {},
};

const productsSlice = createSlice({
  name: 'products',
  initialState,
  reducers: {
    setList(state, action) {
      const { key, items = [], pageInfo = {}, updatedAt = Date.now() } = action.payload || {};
      if (!key) return;
      state.lists[key] = {
        items,
        pageInfo,
        updatedAt,
      };
      if (!state.byKey[key]) {
        state.byKey[key] = { status: 'succeeded', error: null };
      }
    },
    setStatus(state, action) {
      const { key, status } = action.payload || {};
      if (!key) return;
      if (!state.byKey[key]) {
        state.byKey[key] = { status: 'idle', error: null };
      }
      state.byKey[key].status = status;
      if (status !== 'failed') {
        state.byKey[key].error = null;
      }
    },
    setError(state, action) {
      const { key, error } = action.payload || {};
      if (!key) return;
      if (!state.byKey[key]) {
        state.byKey[key] = { status: 'failed', error };
        return;
      }
      state.byKey[key].error = error;
      state.byKey[key].status = 'failed';
    },
    clearEntry(state, action) {
      const { key } = action.payload || {};
      if (!key) return;
      delete state.byKey[key];
      delete state.lists[key];
    },
  },
});

export const { setList, setStatus, setError, clearEntry } = productsSlice.actions;

export const selectListByKey = (state, key) => state?.products?.lists?.[key] || null;
export const selectStatusByKey = (state, key) => state?.products?.byKey?.[key]?.status || 'idle';
export const selectErrorByKey = (state, key) => state?.products?.byKey?.[key]?.error || null;

export default productsSlice.reducer;
