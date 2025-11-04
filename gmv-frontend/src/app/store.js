import { configureStore } from '@reduxjs/toolkit'
import sessionReducer from '../features/platform/auth/sessionSlice.js'
import productsReducer from '../features/products/productsSlice.js'

const PERSIST_KEY = 'gmv_products_cache_v1'
const PERSIST_DEBOUNCE = 500

function loadPersistedProducts() {
  if (typeof window === 'undefined' || !window?.sessionStorage) {
    return undefined
  }
  try {
    const raw = window.sessionStorage.getItem(PERSIST_KEY)
    if (!raw) return undefined
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') {
      return undefined
    }
    return {
      byKey: parsed.byKey && typeof parsed.byKey === 'object' ? parsed.byKey : {},
      lists: parsed.lists && typeof parsed.lists === 'object' ? parsed.lists : {},
    }
  } catch (error) {
    return undefined
  }
}

const persistedProducts = loadPersistedProducts()

export const store = configureStore({
  reducer: {
    session: sessionReducer,
    products: productsReducer,
  },
  preloadedState: persistedProducts ? { products: persistedProducts } : undefined,
  devTools: process.env.NODE_ENV !== 'production'
})

if (typeof window !== 'undefined' && window?.sessionStorage) {
  let persistTimer = null
  let lastSerialized = null
  store.subscribe(() => {
    if (persistTimer) {
      window.clearTimeout(persistTimer)
    }
    persistTimer = window.setTimeout(() => {
      try {
        const state = store.getState()
        const payload = {
          byKey: state.products?.byKey || {},
          lists: state.products?.lists || {},
        }
        const serialized = JSON.stringify(payload)
        if (serialized !== lastSerialized) {
          window.sessionStorage.setItem(PERSIST_KEY, serialized)
          lastSerialized = serialized
        }
      } catch (error) {
        // ignore persistence errors
      }
    }, PERSIST_DEBOUNCE)
  })
}
