import { describe, it, expect } from 'vitest'
import reducer, {
  setBindingGraph,
  toggleBcSelection,
  toggleAdvSelection,
} from '../store/tenantDataSlice.js'

describe('tenantDataSlice selection cascade', () => {
  const graphPayload = {
    bcList: [
      { id: 'bc-1', authId: 'auth-1', name: 'BC 1', summary: { localCount: 2 } },
    ],
    advertisers: [
      { id: 'adv-1', bcId: 'bc-1', authId: 'auth-1', name: 'Adv A' },
    ],
    shops: [
      { id: 'shop-1', bcId: 'bc-1', advertiserId: 'adv-1', authId: 'auth-1', name: 'Shop X' },
    ],
    products: [],
  }

  it('initializes selections based on bindings', () => {
    const state = reducer(undefined, setBindingGraph(graphPayload))
    expect(state.selectedBCIds).toEqual(['bc-1'])
    expect(state.selectedAdvIds).toEqual(['adv-1'])
    expect(state.selectedShopIds).toEqual(['shop-1'])
  })

  it('clears children when BC is toggled off', () => {
    const initial = reducer(undefined, setBindingGraph(graphPayload))
    const next = reducer(initial, toggleBcSelection('bc-1'))
    expect(next.selectedBCIds).toEqual([])
    expect(next.selectedAdvIds).toEqual([])
    expect(next.selectedShopIds).toEqual([])
  })

  it('removes shops when advertiser toggled off', () => {
    const initial = reducer(undefined, setBindingGraph(graphPayload))
    const next = reducer(initial, toggleAdvSelection('adv-1'))
    expect(next.selectedAdvIds).toEqual([])
    expect(next.selectedShopIds).toEqual([])
  })
})

