import { configureStore } from '@reduxjs/toolkit'
import sessionReducer from '../features/platform/auth/sessionSlice.js'

export const store = configureStore({
  reducer: {
    session: sessionReducer,
  },
  devTools: process.env.NODE_ENV !== 'production'
})
