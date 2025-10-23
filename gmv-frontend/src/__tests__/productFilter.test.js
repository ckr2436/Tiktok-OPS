import { describe, it, expect } from './testUtils.js'
import { filterProducts } from '../features/tenants/utils/productFilters.js'

describe('filterProducts', () => {
  const products = [
    { id: '1', shopId: 's1', advertiserId: 'a1', bcId: 'b1', changeType: 'added', status: 'added' },
    { id: '2', shopId: 's2', advertiserId: 'a1', bcId: 'b1', changeType: null, status: 'failed' },
    { id: '3', shopId: 's3', advertiserId: 'a2', bcId: 'b2', changeType: null, status: 'updated' },
  ]

  it('prefers shop selection when provided', () => {
    const result = filterProducts({ products, selectedShopIds: ['s2'] })
    expect(result.map((p) => p.id)).toEqual(['2'])
  })

  it('falls back to advertiser selection when no shops selected', () => {
    const result = filterProducts({ products, selectedAdvIds: ['a2'], selectedShopIds: [] })
    expect(result.map((p) => p.id)).toEqual(['3'])
  })

  it('uses BC selection when no downstream scope specified', () => {
    const result = filterProducts({ products, selectedBCIds: ['b1'], selectedShopIds: [], selectedAdvIds: [] })
    expect(result.map((p) => p.id)).toEqual(['1', '2'])
  })

  it('filters by change-only flag', () => {
    const result = filterProducts({ products, filters: { onlyChanges: true } })
    expect(result.map((p) => p.id)).toEqual(['1', '3'])
  })

  it('filters by failed-only flag', () => {
    const result = filterProducts({ products, filters: { onlyFailed: true } })
    expect(result.map((p) => p.id)).toEqual(['2'])
  })
})
