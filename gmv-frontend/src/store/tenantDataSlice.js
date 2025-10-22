import { createSlice } from '@reduxjs/toolkit'

function toId(value) {
  if (value === undefined || value === null) return ''
  return String(value)
}

function dedupe(values) {
  return Array.from(new Set(values.map(toId)))
}

function cooldownKey(domain, authId) {
  return `${domain || 'unknown'}:${authId ? toId(authId) : 'all'}`
}

const initialState = {
  bcList: [],
  advertisers: [],
  shops: [],
  products: [],
  selectedBCIds: [],
  selectedAdvIds: [],
  selectedShopIds: [],
  showOnlySelected: { bc: false, advertisers: false, shops: false },
  productFilters: { onlyChanges: false, onlyFailed: false },
  lastByDomain: { bc: {}, advertisers: {}, shops: {}, products: {} },
  cooldowns: {},
  loading: {},
  errors: {},
}

function advertisersOf(state, bcId) {
  return state.advertisers.filter((adv) => toId(adv.bcId) === toId(bcId))
}

function shopsOfAdvertiser(state, advId) {
  return state.shops.filter((shop) => toId(shop.advertiserId) === toId(advId))
}

function shopsOfBc(state, bcId) {
  return state.shops.filter((shop) => toId(shop.bcId) === toId(bcId))
}

const slice = createSlice({
  name: 'tenantData',
  initialState,
  reducers: {
    setBindingGraph(state, action) {
      const payload = action.payload || {}
      state.bcList = Array.isArray(payload.bcList) ? payload.bcList : []
      state.advertisers = Array.isArray(payload.advertisers) ? payload.advertisers : []
      state.shops = Array.isArray(payload.shops) ? payload.shops : []
      state.products = Array.isArray(payload.products) ? payload.products : []

      const bcIds = state.bcList.map((bc) => toId(bc.id))
      state.selectedBCIds = bcIds

      const advIds = state.advertisers
        .filter((adv) => state.selectedBCIds.includes(toId(adv.bcId)))
        .map((adv) => toId(adv.id))
      state.selectedAdvIds = dedupe(advIds)

      const shopIds = state.shops
        .filter((shop) => state.selectedAdvIds.includes(toId(shop.advertiserId)))
        .map((shop) => toId(shop.id))
      state.selectedShopIds = dedupe(shopIds)
    },
    toggleBcSelection(state, action) {
      const bcId = toId(action.payload)
      if (!bcId) return
      const isSelected = state.selectedBCIds.includes(bcId)
      if (isSelected) {
        state.selectedBCIds = state.selectedBCIds.filter((id) => id !== bcId)
        const advToRemove = advertisersOf(state, bcId).map((adv) => toId(adv.id))
        state.selectedAdvIds = state.selectedAdvIds.filter((id) => !advToRemove.includes(id))
        const shopToRemove = shopsOfBc(state, bcId).map((shop) => toId(shop.id))
        state.selectedShopIds = state.selectedShopIds.filter((id) => !shopToRemove.includes(id))
      } else {
        state.selectedBCIds = dedupe([...state.selectedBCIds, bcId])
        const advToAdd = advertisersOf(state, bcId).map((adv) => toId(adv.id))
        state.selectedAdvIds = dedupe([...state.selectedAdvIds, ...advToAdd])
        const shopToAdd = shopsOfBc(state, bcId).map((shop) => toId(shop.id))
        state.selectedShopIds = dedupe([...state.selectedShopIds, ...shopToAdd])
      }
    },
    selectAllBcs(state) {
      state.selectedBCIds = state.bcList.map((bc) => toId(bc.id))
      state.selectedAdvIds = state.advertisers.map((adv) => toId(adv.id))
      state.selectedShopIds = state.shops.map((shop) => toId(shop.id))
    },
    clearBcSelection(state) {
      state.selectedBCIds = []
      state.selectedAdvIds = []
      state.selectedShopIds = []
    },
    toggleShowOnlySelected(state, action) {
      const domain = action.payload
      if (!domain || !(domain in state.showOnlySelected)) return
      state.showOnlySelected[domain] = !state.showOnlySelected[domain]
    },
    toggleAdvSelection(state, action) {
      const advId = toId(action.payload)
      if (!advId) return
      const isSelected = state.selectedAdvIds.includes(advId)
      if (isSelected) {
        state.selectedAdvIds = state.selectedAdvIds.filter((id) => id !== advId)
        const shopToRemove = shopsOfAdvertiser(state, advId).map((shop) => toId(shop.id))
        state.selectedShopIds = state.selectedShopIds.filter((id) => !shopToRemove.includes(id))
      } else {
        state.selectedAdvIds = dedupe([...state.selectedAdvIds, advId])
        const shopToAdd = shopsOfAdvertiser(state, advId).map((shop) => toId(shop.id))
        state.selectedShopIds = dedupe([...state.selectedShopIds, ...shopToAdd])
      }
    },
    setSelectedAdvIds(state, action) {
      const values = Array.isArray(action.payload) ? action.payload : []
      state.selectedAdvIds = dedupe(values)
    },
    toggleShopSelection(state, action) {
      const shopId = toId(action.payload)
      if (!shopId) return
      const isSelected = state.selectedShopIds.includes(shopId)
      if (isSelected) {
        state.selectedShopIds = state.selectedShopIds.filter((id) => id !== shopId)
      } else {
        state.selectedShopIds = dedupe([...state.selectedShopIds, shopId])
      }
    },
    setSelectedShopIds(state, action) {
      const values = Array.isArray(action.payload) ? action.payload : []
      state.selectedShopIds = dedupe(values)
    },
    toggleAdvListFilter(state) {
      state.showOnlySelected.advertisers = !state.showOnlySelected.advertisers
    },
    toggleShopListFilter(state) {
      state.showOnlySelected.shops = !state.showOnlySelected.shops
    },
    setProductFilter(state, action) {
      const payload = action.payload || {}
      const key = payload.key
      if (!key || !(key in state.productFilters)) return
      if (payload.value === undefined) {
        state.productFilters[key] = !state.productFilters[key]
      } else {
        state.productFilters[key] = Boolean(payload.value)
      }
    },
    setLastResult(state, action) {
      const payload = action.payload || {}
      const domain = payload.domain
      if (!domain) return
      if (!state.lastByDomain[domain]) state.lastByDomain[domain] = {}
      const authId = payload.authId ? toId(payload.authId) : 'all'
      state.lastByDomain[domain][authId] = payload.data ?? null
    },
    setLoading(state, action) {
      const payload = action.payload || {}
      if (!payload.domain) return
      state.loading[payload.domain] = payload.status ?? 'pending'
    },
    setCooldown(state, action) {
      const payload = action.payload || {}
      const domain = payload.domain
      if (!domain) return
      const key = cooldownKey(domain, payload.authId)
      const until = payload.until
      state.cooldowns[key] = until ? String(until) : null
    },
    clearCooldown(state, action) {
      const payload = action.payload || {}
      const key = cooldownKey(payload.domain, payload.authId)
      delete state.cooldowns[key]
    },
  },
})

export const {
  setBindingGraph,
  toggleBcSelection,
  selectAllBcs,
  clearBcSelection,
  toggleShowOnlySelected,
  toggleAdvSelection,
  setSelectedAdvIds,
  toggleShopSelection,
  setSelectedShopIds,
  toggleAdvListFilter,
  toggleShopListFilter,
  setProductFilter,
  setLastResult,
  setLoading,
  setCooldown,
  clearCooldown,
} = slice.actions

export default slice.reducer

