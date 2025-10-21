// src/features/tenants/bc_ads_shop_product/pages/BcAdsPlanSync.jsx
import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  DEFAULT_PLAN_CONFIG,
  toDisplayConfig,
} from '../../../bc_ads_shop_product/planDefaults.js'
import {
  fetchTenantPlanConfig,
  fetchSyncStatus,
  triggerManualSync,
} from '../service.js'

const LOCAL_KEY_PREFIX = 'bc_ads_plan_sync_last'

function loadLocalLastSync(workspaceId) {
  if (typeof window === 'undefined' || !workspaceId) return ''
  try {
    return window.localStorage.getItem(`${LOCAL_KEY_PREFIX}:${workspaceId}`) || ''
  } catch {
    return ''
  }
}

function saveLocalLastSync(workspaceId, value) {
  if (typeof window === 'undefined' || !workspaceId) return
  try {
    if (value) {
      window.localStorage.setItem(`${LOCAL_KEY_PREFIX}:${workspaceId}`, value)
    } else {
      window.localStorage.removeItem(`${LOCAL_KEY_PREFIX}:${workspaceId}`)
    }
  } catch {
    // 忽略本地存储异常
  }
}

function formatDateTime(value) {
  if (!value) return ''
  try {
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return ''
    const pad = (num) => String(num).padStart(2, '0')
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
  } catch {
    return ''
  }
}

function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return ''
  const totalSeconds = Math.floor(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const parts = []
  if (hours > 0) parts.push(`${hours} 小时`)
  if (minutes > 0) parts.push(`${minutes} 分钟`)
  if (hours === 0 && seconds > 0) parts.push(`${seconds} 秒`)
  return parts.join(' ')
}

function formatStatusLabel(status) {
  const value = String(status || '').toLowerCase()
  if (value === 'success' || value === 'done' || value === 'completed') return '成功'
  if (value === 'failed' || value === 'error') return '失败'
  if (value === 'running' || value === 'pending' || value === 'processing') return '进行中'
  return '未知'
}

function statusTone(status) {
  const value = String(status || '').toLowerCase()
  if (value === 'success' || value === 'done' || value === 'completed') return 'ok'
  if (value === 'failed' || value === 'error') return 'danger'
  if (value === 'running' || value === 'pending' || value === 'processing') return 'warn'
  return 'muted'
}

function sanitizeHistory(list = []) {
  return list.map((item, idx) => ({
    id: item?.id ?? item?.job_id ?? `history-${idx}`,
    triggered_at: item?.triggered_at ?? item?.ran_at ?? item?.synced_at ?? item?.created_at ?? null,
    operator: item?.triggered_by ?? item?.operator ?? item?.user ?? '手动同步',
    status: item?.status ?? item?.result ?? 'pending',
    message: item?.message ?? item?.detail ?? item?.note ?? '',
  }))
}

function normalizeStatus(rawStatus, workspaceId) {
  const localLast = loadLocalLastSync(workspaceId)
  const last = rawStatus?.last_synced_at ?? rawStatus?.lastSyncAt ?? rawStatus?.synced_at ?? localLast
  const next = rawStatus?.next_allowed_at ?? rawStatus?.nextSyncAt ?? ''
  const history = Array.isArray(rawStatus?.history) ? sanitizeHistory(rawStatus.history) : []

  if (last) {
    saveLocalLastSync(workspaceId, last)
  }

  return {
    lastSyncedAt: last || '',
    nextAllowedAt: next || '',
    history,
  }
}

function PlanTimeline({ plans }) {
  if (!Array.isArray(plans) || plans.length === 0) {
    return <div className="plan-empty">暂无可用的任务模板，请联系平台管理员。</div>
  }

  return (
    <ol className="plan-timeline">
      {plans.map((plan, idx) => (
        <li key={plan.id || idx} className="plan-timeline__item">
          <div className="plan-timeline__head">
            <div>
              <div className="plan-timeline__title">{plan.title}</div>
              {plan.objective && <div className="plan-timeline__objective">{plan.objective}</div>}
            </div>
            <div className="plan-timeline__meta">
              {plan.cadence && <span className="plan-chip">{plan.cadence}</span>}
              {plan.audience && <span className="plan-chip plan-chip--muted">适用：{plan.audience}</span>}
            </div>
          </div>

          <div className="plan-timeline__grid">
            {plan.focus && (
              <div>
                <div className="plan-timeline__label">阶段重点</div>
                <p className="plan-timeline__text">{plan.focus}</p>
              </div>
            )}
            {Array.isArray(plan.keyActions) && plan.keyActions.length > 0 && (
              <div>
                <div className="plan-timeline__label">执行要点</div>
                <ul className="plan-timeline__list">
                  {plan.keyActions.map((item, actionIdx) => (
                    <li key={actionIdx}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
            {Array.isArray(plan.deliverables) && plan.deliverables.length > 0 && (
              <div>
                <div className="plan-timeline__label">交付件</div>
                <ul className="plan-timeline__list">
                  {plan.deliverables.map((item, deliverIdx) => (
                    <li key={deliverIdx}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
            {Array.isArray(plan.metrics) && plan.metrics.length > 0 && (
              <div>
                <div className="plan-timeline__label">衡量指标</div>
                <ul className="plan-timeline__list plan-timeline__list--inline">
                  {plan.metrics.map((item, metricIdx) => (
                    <li key={metricIdx}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
            {plan.notes && (
              <div className="plan-timeline__notes">
                <div className="plan-timeline__label">备注</div>
                <p className="plan-timeline__text">{plan.notes}</p>
              </div>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}

function StatusBadge({ status }) {
  const tone = statusTone(status)
  return <span className={`status-badge status-badge--${tone}`}>{formatStatusLabel(status)}</span>
}

export default function BcAdsPlanSync() {
  const { wid } = useParams()
  const workspaceId = wid || ''

  const [config, setConfig] = useState(() => toDisplayConfig(DEFAULT_PLAN_CONFIG))
  const [cooldownMinutes, setCooldownMinutes] = useState(DEFAULT_PLAN_CONFIG.syncCooldownMinutes)
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState('')
  const [lastSyncedAt, setLastSyncedAt] = useState(() => loadLocalLastSync(workspaceId))
  const [nextAllowedAt, setNextAllowedAt] = useState('')
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now())
    }, 1000)
    return () => {
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    setLastSyncedAt(loadLocalLastSync(workspaceId))
    setNextAllowedAt('')
    refreshAll()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId])

  async function refreshAll() {
    if (!workspaceId) return
    setLoading(true)
    setError('')
    try {
      const [planRes, statusRes] = await Promise.all([
        fetchTenantPlanConfig(workspaceId),
        fetchSyncStatus(workspaceId),
      ])
      const normalized = toDisplayConfig(planRes || {})
      setConfig(normalized)
      setCooldownMinutes(normalized.syncCooldownMinutes)
      const status = normalizeStatus(statusRes || {}, workspaceId)
      setLastSyncedAt(status.lastSyncedAt)
      setNextAllowedAt(status.nextAllowedAt)
      setHistory(status.history)
    } catch (err) {
      console.error('Failed to load tenant bc-ads plan', err)
      setConfig(toDisplayConfig(DEFAULT_PLAN_CONFIG))
      const fallback = normalizeStatus({}, workspaceId)
      setLastSyncedAt(fallback.lastSyncedAt)
      setNextAllowedAt(fallback.nextAllowedAt)
      setHistory([])
      setError('加载失败，已展示平台默认模板。')
    } finally {
      setLoading(false)
    }
  }

  const cooldownMs = useMemo(() => Math.max(Number(cooldownMinutes) || 0, 5) * 60 * 1000, [cooldownMinutes])

  const lastSyncTs = useMemo(() => {
    if (!lastSyncedAt) return 0
    const parsed = Date.parse(lastSyncedAt)
    return Number.isNaN(parsed) ? 0 : parsed
  }, [lastSyncedAt])

  const nextAllowedTs = useMemo(() => {
    if (nextAllowedAt) {
      const parsed = Date.parse(nextAllowedAt)
      if (!Number.isNaN(parsed)) return parsed
    }
    if (lastSyncTs && cooldownMs) {
      return lastSyncTs + cooldownMs
    }
    return 0
  }, [nextAllowedAt, lastSyncTs, cooldownMs])

  const remainingMs = useMemo(() => {
    if (!nextAllowedTs) return 0
    return nextAllowedTs - now
  }, [nextAllowedTs, now])

  const isCoolingDown = remainingMs > 0
  const countdownText = formatDuration(remainingMs)
  const lastSyncText = formatDateTime(lastSyncedAt)
  const nextAllowedText = formatDateTime(nextAllowedTs)
  const canSync = !syncing && !isCoolingDown

  async function handleManualSync() {
    if (!workspaceId || !canSync) return
    setSyncing(true)
    setError('')
    try {
      const res = await triggerManualSync(workspaceId)
      const syncedAt = res?.synced_at ?? res?.triggered_at ?? new Date().toISOString()
      setLastSyncedAt(syncedAt)
      saveLocalLastSync(workspaceId, syncedAt)
      if (res?.next_allowed_at) {
        setNextAllowedAt(res.next_allowed_at)
      } else {
        setNextAllowedAt('')
      }
      if (Array.isArray(res?.history)) {
        setHistory(sanitizeHistory(res.history))
      } else {
        setHistory((prev) => [
          {
            id: res?.job_id ?? `manual-${Date.now()}`,
            triggered_at: syncedAt,
            operator: '手动同步',
            status: res?.status ?? 'pending',
            message: res?.message ?? '已触发手动同步任务',
          },
          ...prev,
        ])
      }
      await refreshStatus()
    } catch (err) {
      console.error('Manual sync failed', err)
      setError('同步失败，请稍后重试。')
    } finally {
      setSyncing(false)
    }
  }

  async function refreshStatus() {
    try {
      const statusRes = await fetchSyncStatus(workspaceId)
      const status = normalizeStatus(statusRes || {}, workspaceId)
      setLastSyncedAt(status.lastSyncedAt)
      setNextAllowedAt(status.nextAllowedAt)
      setHistory(status.history)
    } catch (err) {
      console.error('Failed to refresh sync status', err)
    }
  }

  return (
    <div className="page-with-gap">
      <div className="card">
        <div className="sync-header">
          <div className="sync-header__text">
            <h2 style={{ marginTop: 0 }}>BC Ads · 运营计划工作台</h2>
            <p>
              根据平台下发的最新模板执行阶段性任务，保持广告、内容、客服团队节奏一致。
              如需立即获取最新模板，可在冷却期结束后手动同步。
            </p>
            <div className="sync-meta">
              <span>手动同步冷却：{cooldownMinutes} 分钟</span>
              {lastSyncText && <span>上次同步：{lastSyncText}</span>}
              {nextAllowedText && <span>下次可同步：{nextAllowedText}</span>}
            </div>
          </div>
          <div className="sync-header__actions">
            <button type="button" className="btn ghost" onClick={refreshAll} disabled={loading || syncing}>
              {loading ? '刷新中…' : '刷新模板'}
            </button>
            <button type="button" className="btn" onClick={handleManualSync} disabled={!canSync}>
              {syncing ? '同步中…' : isCoolingDown ? `冷却中 · ${countdownText || '请稍候'}` : '手动同步'}
            </button>
          </div>
        </div>
        {error && <div className="alert alert--error">{error}</div>}
        {!error && isCoolingDown && (
          <div className="alert">
            正在冷却中，预计 {nextAllowedText || '稍后'} 可再次触发手动同步。
          </div>
        )}
      </div>

      <div className="card">
        <div className="section-title">阶段任务总览</div>
        {loading ? <div className="plan-loading">模板加载中…</div> : <PlanTimeline plans={config.plans} />}
      </div>

      <div className="card">
        <div className="section-title">同步记录</div>
        {history.length > 0 ? (
          <div className="table-wrap">
            <table className="history-table">
              <thead>
                <tr>
                  <th scope="col">触发时间</th>
                  <th scope="col">触发人</th>
                  <th scope="col">状态</th>
                  <th scope="col">备注</th>
                </tr>
              </thead>
              <tbody>
                {history.map((item) => (
                  <tr key={item.id}>
                    <td>{formatDateTime(item.triggered_at) || '-'}</td>
                    <td>{item.operator || '-'}</td>
                    <td><StatusBadge status={item.status} /></td>
                    <td>{item.message || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="small-muted">暂无手动同步记录。</p>
        )}
      </div>
    </div>
  )
}
