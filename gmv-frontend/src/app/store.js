import { configureStore } from '@reduxjs/toolkit'
import sessionReducer from '../features/platform/auth/sessionSlice.js'
import httpReducer from '../store/httpSlice.js'
import tenantDataReducer from '../store/tenantDataSlice.js'

export const store = configureStore({
  reducer: {
    session: sessionReducer,
    http: httpReducer,
    tenantData: tenantDataReducer,
  },
  devTools: process.env.NODE_ENV !== 'production'
})
