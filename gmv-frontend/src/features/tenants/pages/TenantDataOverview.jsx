import { useCallback, useEffect, useMemo, useState } from 'react'
import { useAppDispatch, useAppSelector } from '../../../app/hooks.js'
import ProductListCard from '../components/ProductListCard.jsx'
import TenantSyncDashboardCard from '../components/TenantSyncDashboardCard.jsx'
import LastResultDialog from '../components/LastResultDialog.jsx'
import syncApi from '../services/syncApi.js'
import {
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
} from '../../../store/tenantDataSlice.js'

const PROVIDER = 'tiktok-business'

function useToasts() {
  const [toasts, setToasts] = useState([])

  const remove = useCallback((id) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== id))
  }, [])

  const push = useCallback(
    ({ type = 'info', message, duration = 3000 }) => {
      if (!message) return
      const id = `toast-${Date.now()}-${Math.random().toString(16).slice(2)}`
      setToasts((prev) => [...prev, { id, type, message }])
      if (duration > 0) {
        setTimeout(() => remove(id), duration)
      }
    },
    [remove]
  )

  return { toasts, push, remove }
}

export function normalizeHeaders(input) {
  const map = {}
  if (!input) return map
  if (typeof input.forEach === 'function') {
    input.forEach((value, key) => {
      map[String(key).toLowerCase()] = Array.isArray(value) ? value[value.length - 1] : value
    })
    return map
  }
  if (typeof input.entries === 'function') {
    for (const [key, value] of input.entries()) {
      map[String(key).toLowerCase()] = Array.isArray(value) ? value[value.length - 1] : value
    }
    return map
  }
  for (const [key, value] of Object.entries(input)) {
    map[String(key).toLowerCase()] = Array.isArray(value) ? value[value.length - 1] : value
  }
  return map
}

export function extractErrorCode(err) {
  return (
    err?.payload?.error?.code ||
    err?.payload?.code ||
    err?.payload?.error_code ||
    (err?.status === 429 ? 'TOO_FREQUENT' : null)
  )
}

function formatCountdown(headers) {
  const retryAfter = Number(headers['retry-after'])
  if (Number.isFinite(retryAfter) && retryAfter > 0) {
    return `${retryAfter} 秒`
  }
  const next = headers['x-next-allowed-at']
  if (next) {
    const ts = Date.parse(next)
    if (!Number.isNaN(ts)) {
      const diff = Math.ceil((ts - Date.now()) / 1000)
      if (diff > 0) return `${diff} 秒`
    }
  }
  return '稍后'
}

export function aggregateDiffTotals(results) {
  const totals = { added: 0, removed: 0, updated: 0 }
  if (!results) return totals
  Object.values(results).forEach((entry) => {
    const diff = entry?.summary?.diff
    if (!diff) return
    totals.added += Number(diff.added ?? 0)
    totals.removed += Number(diff.removed ?? 0)
    totals.updated += Number(diff.updated ?? 0)
  })
  return totals
}

export default function TenantDataOverview() {
  const dispatch = useAppDispatch()
  const workspaceId = useAppSelector((s) => s.session?.data?.workspace_id)
  const {
    bcList,
    advertisers,
    shops,
    products,
    selectedBCIds,
    selectedAdvIds,
    selectedShopIds,
    showOnlySelected,
    productFilters,
    lastByDomain,
    cooldowns,
    loading,
  } = useAppSelector((s) => s.tenantData)
  const { lastRequestId } = useAppSelector((s) => s.http)

  const { toasts, push: pushToast, remove: removeToast } = useToasts()
  const [loadingBindings, setLoadingBindings] = useState(false)
  const [dialogState, setDialogState] = useState({ open: false, domain: null, authId: null })

  const bcMap = useMemo(() => {
    const map = {}
    bcList.forEach((bc) => {
      map[bc.id] = bc
    })
    return map
  }, [bcList])

  const bcByAuth = useMemo(() => {
    const map = {}
    bcList.forEach((bc) => {
      if (bc.authId) map[bc.authId] = bc
    })
    return map
  }, [bcList])

  const advMap = useMemo(() => {
    const map = {}
    advertisers.forEach((adv) => {
      map[adv.id] = adv
    })
    return map
  }, [advertisers])

  const shopMap = useMemo(() => {
    const map = {}
    shops.forEach((shop) => {
      map[shop.id] = shop
    })
    return map
  }, [shops])

  const cooldownForDomain = useCallback(
    (domain) => {
      const map = {}
      Object.entries(cooldowns).forEach(([key, value]) => {
        const [d, auth] = key.split(':')
        if (d === domain && value) map[auth] = value
      })
      return map
    },
    [cooldowns]
  )

  const bcCooldownMap = useMemo(() => cooldownForDomain('bc'), [cooldownForDomain])
  const advCooldownMap = useMemo(() => cooldownForDomain('advertisers'), [cooldownForDomain])
  const shopCooldownMap = useMemo(() => cooldownForDomain('shops'), [cooldownForDomain])
  const productCooldownMap = useMemo(() => cooldownForDomain('products'), [cooldownForDomain])

  const bcCooldownUntil = useMemo(() => {
    let earliest = null
    selectedBCIds.forEach((id) => {
      const authId = bcMap[id]?.authId
      const raw = authId ? bcCooldownMap[authId] : null
      if (!raw) return
      const ts = Date.parse(raw)
      if (Number.isNaN(ts)) return
      if (earliest === null || ts < earliest) earliest = ts
    })
    return earliest
  }, [selectedBCIds, bcMap, bcCooldownMap])

  const advCooldownUntil = useMemo(() => {
    let earliest = null
    const advPool = selectedAdvIds.length
      ? selectedAdvIds
      : advertisers.filter((adv) => selectedBCIds.includes(adv.bcId)).map((adv) => adv.id)
    advPool.forEach((id) => {
      const authId = advMap[id]?.authId
      const raw = authId ? advCooldownMap[authId] : null
      if (!raw) return
      const ts = Date.parse(raw)
      if (Number.isNaN(ts)) return
      if (earliest === null || ts < earliest) earliest = ts
    })
    return earliest
  }, [selectedAdvIds, advertisers, selectedBCIds, advMap, advCooldownMap])

  const shopCooldownUntil = useMemo(() => {
    let earliest = null
    const shopPool = selectedShopIds.length
      ? selectedShopIds
      : shops.filter((shop) => selectedBCIds.includes(shop.bcId)).map((shop) => shop.id)
    shopPool.forEach((id) => {
      const authId = shopMap[id]?.authId
      const raw = authId ? shopCooldownMap[authId] : null
      if (!raw) return
      const ts = Date.parse(raw)
      if (Number.isNaN(ts)) return
      if (earliest === null || ts < earliest) earliest = ts
    })
    return earliest
  }, [selectedShopIds, shops, selectedBCIds, shopMap, shopCooldownMap])

  const lastByDomainOrEmpty = (domain) => lastByDomain?.[domain] || {}

  const applyCooldown = useCallback(
    (domain, authIds, headers) => {
      if (!authIds.length) return
      const normalized = normalizeHeaders(headers)
      let target = null
      const nextAllowed = normalized['x-next-allowed-at']
      if (nextAllowed) {
        const ts = Date.parse(nextAllowed)
        if (!Number.isNaN(ts)) target = new Date(ts).toISOString()
      }
      if (!target) {
        const retryAfter = Number(normalized['retry-after'])
        if (Number.isFinite(retryAfter) && retryAfter > 0) {
          target = new Date(Date.now() + retryAfter * 1000).toISOString()
        }
      }
      if (target) {
        authIds.forEach((id) => dispatch(setCooldown({ domain, authId: id, until: target })))
      }
    },
    [dispatch]
  )

  const loadLastForAuths = useCallback(
    async (domain, authIds) => {
      for (const authId of authIds) {
        try {
          const data = await syncApi.getLast({ workspaceId, provider: PROVIDER, domain, authId })
          dispatch(setLastResult({ domain, authId, data }))
          if (data?.nextAllowedAt) {
            dispatch(setCooldown({ domain, authId, until: data.nextAllowedAt }))
          } else {
            dispatch(clearCooldown({ domain, authId }))
          }
        } catch (error) {
          // ignore missing history
        }
      }
    },
    [workspaceId, dispatch]
  )

  useEffect(() => {
    if (!workspaceId) return
    let cancelled = false
    const run = async () => {
      setLoadingBindings(true)
      try {
        const graph = await syncApi.listBindings({ workspaceId, provider: PROVIDER })
        if (cancelled) return
        dispatch(setBindingGraph(graph))
        const authIds = graph.bcList.map((bc) => bc.authId).filter(Boolean)
        for (const domain of ['bc', 'advertisers', 'shops', 'products']) {
          await loadLastForAuths(domain, authIds)
          if (cancelled) break
        }
      } catch (error) {
        pushToast({ type: 'error', message: error?.message || '加载失败' })
      } finally {
        if (!cancelled) setLoadingBindings(false)
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [workspaceId, dispatch, loadLastForAuths, pushToast])

  const getAuthIdsForDomain = useCallback(
    (domain) => {
      if (domain === 'bc') {
        return selectedBCIds
          .map((id) => bcMap[id]?.authId)
          .filter((value) => Boolean(value))
      }
      if (domain === 'advertisers') {
        const base = selectedAdvIds.length
          ? selectedAdvIds
          : advertisers.filter((adv) => selectedBCIds.includes(adv.bcId)).map((adv) => adv.id)
        const ids = new Set()
        base.forEach((id) => {
          const adv = advMap[id]
          if (adv?.authId) ids.add(adv.authId)
        })
        return Array.from(ids)
      }
      if (domain === 'shops') {
        const base = selectedShopIds.length
          ? selectedShopIds
          : shops.filter((shop) => selectedBCIds.includes(shop.bcId)).map((shop) => shop.id)
        const ids = new Set()
        base.forEach((id) => {
          const shop = shopMap[id]
          if (shop?.authId) ids.add(shop.authId)
        })
        return Array.from(ids)
      }
      if (domain === 'products') {
        const scope = []
        if (selectedShopIds.length > 0) {
          selectedShopIds.forEach((id) => {
            const shop = shopMap[id]
            if (shop?.authId) scope.push(shop.authId)
          })
        } else if (selectedAdvIds.length > 0) {
          selectedAdvIds.forEach((id) => {
            const adv = advMap[id]
            if (adv?.authId) scope.push(adv.authId)
          })
        } else {
          selectedBCIds.forEach((id) => {
            const bc = bcMap[id]
            if (bc?.authId) scope.push(bc.authId)
          })
        }
        return Array.from(new Set(scope))
      }
      return []
    },
    [selectedBCIds, selectedAdvIds, selectedShopIds, bcMap, advMap, shopMap, advertisers, shops]
  )

  const handleSync = useCallback(
    async (domain) => {
      if (!workspaceId) return
      const authIds = getAuthIdsForDomain(domain)
      if (!authIds.length) {
        pushToast({ type: 'info', message: '请先选择需要同步的范围' })
        return
      }
      dispatch(setLoading({ domain, status: 'pending' }))
      let proceedToPoll = true
      try {
        await syncApi.postSync({ workspaceId, provider: PROVIDER, domain, authIds })
      } catch (error) {
        const headers = normalizeHeaders(error?.headers)
        const code = extractErrorCode(error)
        if (code === 'IDEMPOTENCY_CONFLICT') {
          pushToast({ type: 'info', message: '系统已收到相同请求，结果即将更新', duration: 3500 })
          applyCooldown(domain, authIds, headers)
        } else {
          proceedToPoll = false
          applyCooldown(domain, authIds, headers)
          let message = error?.message || '同步失败'
          if (code === 'TOO_FREQUENT') {
            message = `操作过于频繁，${formatCountdown(headers)} 后可重试`
          } else if (code === 'OAUTH_EXPIRED') {
            message = '授权已过期，请重新授权'
          } else if (code === 'PERMISSION_DENIED') {
            message = '权限不足，无法同步'
          } else if (code === 'REMOTE_ERROR') {
            message = '上游暂不可用，请稍后重试'
          }
          pushToast({ type: 'error', message })
        }
      }

      if (!proceedToPoll) {
        dispatch(setLoading({ domain, status: 'idle' }))
        return
      }

      try {
        const poll = await syncApi.pollLastUntilSettled({
          workspaceId,
          provider: PROVIDER,
          domain,
          authIds,
          onUpdate: ({ authId, data }) => {
            dispatch(setLastResult({ domain, authId, data }))
          },
        })
        Object.entries(poll.results || {}).forEach(([authId, data]) => {
          dispatch(setLastResult({ domain, authId, data }))
          if (data?.nextAllowedAt) {
            dispatch(setCooldown({ domain, authId, until: data.nextAllowedAt }))
          } else {
            dispatch(clearCooldown({ domain, authId }))
          }
        })
        if (poll.status === 'timeout') {
          pushToast({ type: 'warning', message: '同步结果更新超时，请稍后查看' })
        } else {
          const totals = aggregateDiffTotals(poll.results)
          pushToast({
            type: 'success',
            message: `同步完成：+${totals.added} / -${totals.removed} / ~${totals.updated}`,
            duration: 3200,
          })
        }
      } catch (error) {
        const headers = normalizeHeaders(error?.headers)
        applyCooldown(domain, authIds, headers)
        pushToast({ type: 'error', message: error?.message || '同步状态刷新失败' })
      } finally {
        dispatch(setLoading({ domain, status: 'idle' }))
      }
    },
    [workspaceId, getAuthIdsForDomain, dispatch, applyCooldown, pushToast]
  )

  const handleOpenDialog = useCallback((domain, authId) => {
    setDialogState({ open: true, domain, authId })
  }, [])

  const handleCloseDialog = useCallback(() => {
    setDialogState({ open: false, domain: null, authId: null })
  }, [])

  const dialogResult = dialogState.open
    ? lastByDomainOrEmpty(dialogState.domain)[dialogState.authId] || null
    : null

  const advVisibleIds = useMemo(
    () => advertisers.filter((adv) => selectedBCIds.includes(adv.bcId)).map((adv) => adv.id),
    [advertisers, selectedBCIds]
  )

  const shopVisibleIds = useMemo(
    () => shops.filter((shop) => selectedBCIds.includes(shop.bcId)).map((shop) => shop.id),
    [shops, selectedBCIds]
  )

  const handleAdvSelectAll = useCallback(() => {
    dispatch(setSelectedAdvIds(advVisibleIds))
  }, [dispatch, advVisibleIds])

  const handleAdvClear = useCallback(() => {
    dispatch(setSelectedAdvIds([]))
  }, [dispatch])

  const handleShopSelectAll = useCallback(() => {
    dispatch(setSelectedShopIds(shopVisibleIds))
  }, [dispatch, shopVisibleIds])

  const handleShopClear = useCallback(() => {
    dispatch(setSelectedShopIds([]))
  }, [dispatch])

  const productScopeAuthIds = useMemo(
    () => getAuthIdsForDomain('products'),
    [getAuthIdsForDomain]
  )

  return (
    <div className="data-page">
      {loadingBindings && <div className="loading-overlay">数据加载中…</div>}

      <div className="data-page__cards">
        <TenantSyncDashboardCard
          bcList={bcList}
          advertisers={advertisers}
          shops={shops}
          bcMap={bcMap}
          advMap={advMap}
          selectedBCIds={selectedBCIds}
          selectedAdvIds={selectedAdvIds}
          selectedShopIds={selectedShopIds}
          showOnlySelected={showOnlySelected}
          onToggleBc={(id) => dispatch(toggleBcSelection(id))}
          onToggleAdv={(id) => dispatch(toggleAdvSelection(id))}
          onToggleShop={(id) => dispatch(toggleShopSelection(id))}
          onSelectAllBcs={() => dispatch(selectAllBcs())}
          onClearBcs={() => dispatch(clearBcSelection())}
          onSelectAllAdvs={handleAdvSelectAll}
          onClearAdvs={handleAdvClear}
          onSelectAllShops={handleShopSelectAll}
          onClearShops={handleShopClear}
          onToggleShowOnlyBc={() => dispatch(toggleShowOnlySelected('bc'))}
          onToggleShowOnlyAdv={() => dispatch(toggleAdvListFilter())}
          onToggleShowOnlyShop={() => dispatch(toggleShopListFilter())}
          onSyncBc={() => handleSync('bc')}
          onSyncAdvertisers={() => handleSync('advertisers')}
          onSyncShops={() => handleSync('shops')}
          loadingBc={loading.bc === 'pending'}
          loadingAdvertisers={loading.advertisers === 'pending'}
          loadingShops={loading.shops === 'pending'}
          cooldownBc={bcCooldownUntil}
          cooldownAdvertisers={advCooldownUntil}
          cooldownShops={shopCooldownUntil}
          cooldownMapBc={bcCooldownMap}
          cooldownMapAdvertisers={advCooldownMap}
          cooldownMapShops={shopCooldownMap}
          lastByDomain={lastByDomain}
          onShowDetail={handleOpenDialog}
          getAuthIdsForDomain={getAuthIdsForDomain}
          onRefreshLast={loadLastForAuths}
        />

        <ProductListCard
          products={products}
          shopMap={shopMap}
          advMap={advMap}
          bcMap={bcMap}
          selectedShopIds={selectedShopIds}
          selectedAdvIds={selectedAdvIds}
          selectedBCIds={selectedBCIds}
          filters={productFilters}
          onToggleFilter={(key) => dispatch(setProductFilter({ key }))}
          onSync={() => handleSync('products')}
          loading={loading.products === 'pending'}
          cooldownMap={productCooldownMap}
          lastByAuth={lastByDomainOrEmpty('products')}
          syncDisabled={!productScopeAuthIds.length}
        />
      </div>

      {dialogState.open && (
        <LastResultDialog
          open={dialogState.open}
          domain={dialogState.domain}
          result={dialogResult}
          requestId={lastRequestId}
          onClose={handleCloseDialog}
        />
      )}

      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast--${toast.type}`}>
            <span>{toast.message}</span>
            <button type="button" className="toast__close" onClick={() => removeToast(toast.id)}>
              ×
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

