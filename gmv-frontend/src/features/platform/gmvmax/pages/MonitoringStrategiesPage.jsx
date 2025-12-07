import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import FormField from '../../../../components/ui/FormField.jsx'
import Loading from '../../../../components/ui/Loading.jsx'
import Modal from '../../../../components/ui/Modal.jsx'
import {
  MONITORING_STRATEGY_LEVELS,
  PROMOTION_TYPES,
  STRATEGY_CATEGORIES,
  TASK_OPTIONS_BY_CATEGORY,
  getCategoryLabel,
  getTaskLabel,
} from '../constants.js'
import {
  createMonitoringStrategy,
  deleteMonitoringStrategy,
  disableMonitoringStrategy,
  enableMonitoringStrategy,
  listMonitoringStrategies,
  updateMonitoringStrategy,
} from '../service.js'

function formatDate(value) {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleString()
  } catch (err) {
    return String(value)
  }
}

function getDefaultTaskName(category) {
  const opts = TASK_OPTIONS_BY_CATEGORY[category]
  return opts && opts.length > 0 ? opts[0].value : ''
}

function formatParamsSummary(params = {}) {
  const entries = Object.entries(params || {})
  if (!entries.length) return '-'
  return entries
    .map(([key, value]) => `${key}=${value}`)
    .join(', ')
}

function paramsForCategory(category, taskName, paramsJson, limitInput) {
  if (category !== 'TTB_BASE') return paramsJson || {}
  const payload = {
    mode: paramsJson?.mode || 'incremental',
  }
  if (limitInput !== '' && limitInput !== null && limitInput !== undefined) {
    payload.limit = Number(limitInput)
  } else if (paramsJson?.limit) {
    payload.limit = paramsJson.limit
  }
  return payload
}

function StrategyFormModal({ open, mode, onClose, onSubmit, initial }) {
  const defaultCategory = initial?.category || 'GMVMAX'
  const defaultTask = initial?.task_name || getDefaultTaskName(defaultCategory)

  const [workspaceId, setWorkspaceId] = useState(initial?.workspace_id || '')
  const [authId, setAuthId] = useState(initial?.auth_id || '')
  const [advertiserId, setAdvertiserId] = useState(initial?.advertiser_id || '')
  const [storeId, setStoreId] = useState(initial?.store_id || '')
  const [promotionType, setPromotionType] = useState(initial?.promotion_type || '')
  const [level, setLevel] = useState(initial?.level || MONITORING_STRATEGY_LEVELS[0])
  const [intervalMinutes, setIntervalMinutes] = useState(initial?.interval_minutes || '')
  const [maxCampaigns, setMaxCampaigns] = useState(initial?.max_campaigns_per_run ?? '')
  const [enabled, setEnabled] = useState(initial?.enabled ?? true)
  const [category, setCategory] = useState(defaultCategory)
  const [taskName, setTaskName] = useState(defaultTask)
  const [paramsMode, setParamsMode] = useState(initial?.params_json?.mode || 'incremental')
  const [paramsLimit, setParamsLimit] = useState(
    initial?.params_json?.limit ?? ''
  )
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)

  useEffect(() => {
    if (!open) return
    const nextCategory = initial?.category || 'GMVMAX'
    const nextTask = initial?.task_name || getDefaultTaskName(nextCategory)
    setError('')
    setWorkspaceId(initial?.workspace_id || '')
    setAuthId(initial?.auth_id || '')
    setAdvertiserId(initial?.advertiser_id || '')
    setStoreId(initial?.store_id || '')
    setPromotionType(initial?.promotion_type || '')
    setLevel(initial?.level || MONITORING_STRATEGY_LEVELS[0])
    setIntervalMinutes(initial?.interval_minutes || '')
    setMaxCampaigns(initial?.max_campaigns_per_run ?? '')
    setEnabled(initial?.enabled ?? true)
    setCategory(nextCategory)
    setTaskName(nextTask)
    setParamsMode(initial?.params_json?.mode || 'incremental')
    setParamsLimit(initial?.params_json?.limit ?? '')
  }, [open, initial])

  const disabledBase = mode === 'edit'

  const taskOptions = TASK_OPTIONS_BY_CATEGORY[category] || []

  const handleCategoryChange = (nextCategory) => {
    setCategory(nextCategory)
    const defaultTaskName = getDefaultTaskName(nextCategory)
    setTaskName(defaultTaskName)
    if (nextCategory !== 'TTB_BASE') {
      setParamsMode('incremental')
      setParamsLimit('')
    }
  }

  const handleSubmit = async (event) => {
    event.preventDefault()
    setError('')

    const interval = Number(intervalMinutes)
    if (!interval || interval <= 0) {
      setError('同步间隔必须是正整数')
      return
    }

    if (category === 'GMVMAX' && !level) {
      setError('GMVMAX 策略需要选择 level')
      return
    }

    const paramsJson =
      category === 'TTB_BASE'
        ? (() => {
            if (!paramsMode || !['incremental', 'full'].includes(paramsMode)) {
              setError('请选择有效的任务模式')
              return null
            }
            if (paramsLimit !== '' && (Number(paramsLimit) <= 0 || !Number.isInteger(Number(paramsLimit)))) {
              setError('limit 需为大于 0 的整数')
              return null
            }
            const payload = { mode: paramsMode }
            if (paramsLimit !== '') {
              payload.limit = Number(paramsLimit)
            }
            return payload
          })()
        : paramsForCategory(category, taskName, initial?.params_json || {}, paramsLimit)

    if (paramsJson === null) return

    const payloadBase = {
      category,
      task_name: taskName,
      interval_minutes: interval,
      max_campaigns_per_run: maxCampaigns === '' ? null : Number(maxCampaigns),
      enabled,
      params_json: paramsJson,
    }

    const gmvmPayload =
      category === 'GMVMAX'
        ? {
            promotion_type: promotionType || null,
            level,
          }
        : {
            promotion_type: null,
            level: null,
          }

    const payload = mode === 'edit'
      ? {
          ...payloadBase,
          ...gmvmPayload,
        }
      : {
          workspace_id: Number(workspaceId),
          auth_id: authId === '' ? null : Number(authId),
          advertiser_id: advertiserId || null,
          store_id: storeId || null,
          ...payloadBase,
          ...gmvmPayload,
        }

    if (mode !== 'edit') {
      if (!payload.workspace_id) {
        setError('workspace_id 不能为空')
        return
      }
    }

    try {
      setPending(true)
      await onSubmit(payload)
    } catch (err) {
      const resp = err?.response
      const detail = resp?.data?.detail || resp?.data?.message
      const code = resp?.data?.code

      if (code === 'INVALID_INTERVAL') {
        setError('同步间隔必须大于 0 分钟')
      } else if (code === 'LEVEL_REQUIRED') {
        setError('GMVMAX 策略需要指定 level')
      } else if (resp?.status === 409 && code === 'DUPLICATE_STRATEGY') {
        setError('相同 workspace / 维度 的策略已存在，请不要重复创建。')
      } else if (code === 'PARAMS_INVALID') {
        setError(detail || '任务参数校验失败')
      } else if (detail) {
        setError(detail)
      } else {
        setError(err?.message || '保存失败')
      }
    } finally {
      setPending(false)
    }
  }

  return (
    <Modal open={open} title={mode === 'edit' ? '编辑策略' : '新建策略'} onClose={pending ? undefined : onClose}>
      <form onSubmit={handleSubmit} className="form-grid" style={{gap:16}}>
        <div className="section">
          <h4 style={{margin:'8px 0'}}>基础范围</h4>
          <div className="form-grid" style={{gridTemplateColumns:'repeat(auto-fit, minmax(200px, 1fr))'}}>
            <FormField label="workspace_id" required error={error && !workspaceId && mode !== 'edit'}>
              <input
                className="input"
                type="number"
                value={workspaceId}
                onChange={(e) => setWorkspaceId(e.target.value)}
                disabled={disabledBase}
                placeholder="必填"
              />
            </FormField>
            <FormField label="auth_id">
              <input
                className="input"
                type="number"
                value={authId}
                onChange={(e) => setAuthId(e.target.value)}
                disabled={disabledBase}
                placeholder="可空（为空表示该 workspace 下所有授权）"
              />
            </FormField>
            <FormField label="advertiser_id">
              <input
                className="input"
                value={advertiserId}
                onChange={(e) => setAdvertiserId(e.target.value)}
                disabled={disabledBase}
                placeholder="可空（为空表示所有广告主）"
              />
            </FormField>
            <FormField label="store_id">
              <input
                className="input"
                value={storeId}
                onChange={(e) => setStoreId(e.target.value)}
                disabled={disabledBase}
                placeholder="可空（为空表示所有店铺）"
              />
            </FormField>
          </div>
        </div>

        <div className="section" style={{gridColumn:'1/-1'}}>
          <h4 style={{margin:'8px 0'}}>任务类别 / 类型</h4>
          <div className="form-grid" style={{gridTemplateColumns:'repeat(auto-fit, minmax(200px, 1fr))'}}>
            <FormField label="category" required>
              <select
                className="input"
                value={category}
                onChange={(e) => handleCategoryChange(e.target.value)}
              >
                {STRATEGY_CATEGORIES.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </FormField>
            {category === 'GMVMAX' ? (
              <FormField label="task_name">
                <input className="input" value="gmvmax.strategy" disabled />
              </FormField>
            ) : (
              <FormField label="task_name" required>
                <select
                  className="input"
                  value={taskName}
                  onChange={(e) => setTaskName(e.target.value)}
                >
                  {taskOptions.map((opt) => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              </FormField>
            )}
          </div>
        </div>

        <div className="section" style={{gridColumn:'1/-1'}}>
          <h4 style={{margin:'8px 0'}}>调度频率</h4>
          <div className="form-grid" style={{gridTemplateColumns:'repeat(auto-fit, minmax(200px, 1fr))'}}>
            <FormField label="interval_minutes" required error={error?.includes('间隔') ? error : undefined}>
              <input
                className="input"
                type="number"
                min={1}
                value={intervalMinutes}
                onChange={(e) => setIntervalMinutes(e.target.value)}
                placeholder="同步间隔（分钟）"
              />
            </FormField>

            <FormField label="max_campaigns_per_run">
              <input
                className="input"
                type="number"
                value={maxCampaigns}
                onChange={(e) => setMaxCampaigns(e.target.value)}
                placeholder="可空"
              />
            </FormField>

            <FormField label="enabled">
              <label style={{display:'flex', alignItems:'center', gap:8}}>
                <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                <span>{enabled ? '启用' : '停用'}</span>
              </label>
            </FormField>
          </div>
        </div>

        {category === 'GMVMAX' && (
          <div className="section" style={{gridColumn:'1/-1'}}>
            <h4 style={{margin:'8px 0'}}>GMVMAX 维度</h4>
            <div className="form-grid" style={{gridTemplateColumns:'repeat(auto-fit, minmax(200px, 1fr))'}}>
              <FormField label="promotion_type">
                <select className="input" value={promotionType} onChange={(e) => setPromotionType(e.target.value)}>
                  <option value="">（空）</option>
                  {PROMOTION_TYPES.map((opt) => (
                    <option key={opt} value={opt}>{opt}</option>
                  ))}
                </select>
              </FormField>

              <FormField label="level" required>
                <select className="input" value={level} onChange={(e) => setLevel(e.target.value)}>
                  {MONITORING_STRATEGY_LEVELS.map((lvl) => (
                    <option key={lvl} value={lvl}>{lvl}</option>
                  ))}
                </select>
              </FormField>
            </div>
          </div>
        )}

        <div className="section" style={{gridColumn:'1/-1'}}>
          <h4 style={{margin:'8px 0'}}>任务参数</h4>
          {category === 'TTB_BASE' ? (
            <div className="form-grid" style={{gridTemplateColumns:'repeat(auto-fit, minmax(200px, 1fr))'}}>
              <FormField label="mode" required>
                <select
                  className="input"
                  value={paramsMode}
                  onChange={(e) => setParamsMode(e.target.value)}
                >
                  <option value="incremental">incremental</option>
                  <option value="full">full</option>
                </select>
              </FormField>
              <FormField label="limit" hint="可空；大于 0 的整数">
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={paramsLimit}
                  onChange={(e) => setParamsLimit(e.target.value)}
                  placeholder="例如 500"
                />
              </FormField>
            </div>
          ) : (
            <div style={{padding:'6px 4px', color:'var(--muted)'}}>无需额外参数，按 level / promotion_type 控制策略。</div>
          )}
        </div>

        {error && <div className="form-error" style={{gridColumn:'1/-1'}}>{error}</div>}

        <div style={{display:'flex', justifyContent:'flex-end', gap:10, gridColumn:'1/-1', marginTop:6}}>
          <button type="button" className="btn ghost" onClick={onClose} disabled={pending}>取消</button>
          <button type="submit" className="btn" disabled={pending}>{pending ? '提交中…' : '保存'}</button>
        </div>
      </form>
    </Modal>
  )
}

export default function MonitoringStrategiesPage() {
  const [filters, setFilters] = useState({
    workspace_id: '',
    auth_id: '',
    store_id: '',
    promotion_type: '',
    level: '',
    enabled: '',
    category: '',
    task_name: '',
  })
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState(null)
  const [toast, setToast] = useState('')

  const queryClient = useQueryClient()

  const taskFilterOptions = useMemo(() => {
    const cat = filters.category || 'GMVMAX'
    return TASK_OPTIONS_BY_CATEGORY[cat] || []
  }, [filters.category])

  const queryParams = useMemo(() => {
    const params = { limit: 50, offset: 0 }
    if (filters.workspace_id) params.workspace_id = Number(filters.workspace_id)
    if (filters.auth_id) params.auth_id = Number(filters.auth_id)
    if (filters.store_id) params.store_id = filters.store_id
    if (filters.promotion_type) params.promotion_type = filters.promotion_type
    if (filters.level) params.level = filters.level
    if (filters.enabled === 'true') params.enabled = true
    if (filters.enabled === 'false') params.enabled = false
    if (filters.category) params.category = filters.category
    if (filters.task_name) params.task_name = filters.task_name
    return params
  }, [filters])

  const strategiesQuery = useQuery({
    queryKey: ['platform', 'gmvmax', 'monitoring-strategies', queryParams],
    queryFn: () => listMonitoringStrategies(queryParams),
    keepPreviousData: true,
  })

  const invalidateList = () => queryClient.invalidateQueries({ queryKey: ['platform', 'gmvmax', 'monitoring-strategies'] })

  const createMutation = useMutation({
    mutationFn: createMonitoringStrategy,
    onSuccess: () => {
      setToast('创建成功')
      setShowForm(false)
      invalidateList()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }) => updateMonitoringStrategy(id, payload),
    onSuccess: () => {
      setToast('更新成功')
      setEditing(null)
      invalidateList()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: deleteMonitoringStrategy,
    onSuccess: () => {
      setToast('已删除')
      invalidateList()
    },
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }) => (enabled ? enableMonitoringStrategy(id) : disableMonitoringStrategy(id)),
    onSuccess: () => invalidateList(),
  })

  const handleSubmit = async (payload) => {
    if (editing) {
      await updateMutation.mutateAsync({ id: editing.id, payload })
    } else {
      await createMutation.mutateAsync(payload)
    }
  }

  const rows = strategiesQuery.data?.items ?? []
  const total = strategiesQuery.data?.total ?? 0
  const loading = strategiesQuery.isLoading
  const saving = createMutation.isLoading || updateMutation.isLoading

  return (
    <div className="card card--elevated">
      <div style={{display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:12}}>
        <div>
          <h3 style={{margin:0}}>策略调度中心</h3>
          <p className="small-muted" style={{margin:0}}>统一管理 GMVMAX 指标策略与 TikTok 基础同步任务。</p>
        </div>
        <div style={{display:'flex', gap:8}}>
          <button className="btn ghost" onClick={() => strategiesQuery.refetch()} disabled={strategiesQuery.isFetching}>刷新</button>
          <button className="btn" onClick={() => setShowForm(true)} disabled={saving}>新建策略</button>
        </div>
      </div>

      <div className="card" style={{marginBottom:12}}>
        <div className="form-grid" style={{gridTemplateColumns:'repeat(auto-fit, minmax(160px, 1fr))', gap:12}}>
          <FormField label="workspace_id">
            <input className="input" value={filters.workspace_id} onChange={(e) => setFilters({ ...filters, workspace_id: e.target.value })} placeholder="全部" />
          </FormField>
          <FormField label="auth_id">
            <input className="input" value={filters.auth_id} onChange={(e) => setFilters({ ...filters, auth_id: e.target.value })} placeholder="全部" />
          </FormField>
          <FormField label="store_id">
            <input className="input" value={filters.store_id} onChange={(e) => setFilters({ ...filters, store_id: e.target.value })} placeholder="全部" />
          </FormField>
          <FormField label="promotion_type">
            <select className="input" value={filters.promotion_type} onChange={(e) => setFilters({ ...filters, promotion_type: e.target.value })}>
              <option value="">全部</option>
              {PROMOTION_TYPES.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
            </select>
          </FormField>
          <FormField label="level">
            <select className="input" value={filters.level} onChange={(e) => setFilters({ ...filters, level: e.target.value })}>
              <option value="">全部</option>
              {MONITORING_STRATEGY_LEVELS.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
            </select>
          </FormField>
          <FormField label="category">
            <select
              className="input"
              value={filters.category}
              onChange={(e) => setFilters({ ...filters, category: e.target.value, task_name: '' })}
            >
              <option value="">全部</option>
              {STRATEGY_CATEGORIES.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </FormField>
          <FormField label="task_name">
            <select
              className="input"
              value={filters.task_name}
              onChange={(e) => setFilters({ ...filters, task_name: e.target.value })}
              disabled={!filters.category && filters.task_name === '' && !taskFilterOptions.length}
            >
              <option value="">全部</option>
              {taskFilterOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </FormField>
          <FormField label="enabled">
            <select className="input" value={filters.enabled} onChange={(e) => setFilters({ ...filters, enabled: e.target.value })}>
              <option value="">全部</option>
              <option value="true">仅启用</option>
              <option value="false">仅停用</option>
            </select>
          </FormField>
        </div>
      </div>

      {toast && <div className="alert" style={{marginBottom:12}}>{toast}</div>}
      {strategiesQuery.error && <div className="alert alert--error" style={{marginBottom:12}}>{strategiesQuery.error?.message || '加载失败'}</div>}

      <div className="table-wrap" style={{border:'1px solid var(--border)', borderRadius:12}}>
        <table style={{width:'100%', borderCollapse:'collapse', minWidth:1400}}>
          <thead style={{background:'var(--panel-2)'}}>
            <tr>
              <Th>ID</Th>
              <Th>category</Th>
              <Th>task_name</Th>
              <Th>params</Th>
              <Th>workspace_id</Th>
              <Th>auth_id</Th>
              <Th>advertiser_id</Th>
              <Th>store_id</Th>
              <Th>promotion_type</Th>
              <Th>level</Th>
              <Th>interval_minutes</Th>
              <Th>max_campaigns_per_run</Th>
              <Th>enabled</Th>
              <Th>last_run_at</Th>
              <Th>last_success_at</Th>
              <Th>last_error_at</Th>
              <Th>last_error</Th>
              <Th>updated_at</Th>
              <Th style={{width:140}}>操作</Th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={19} style={{padding:18}}><Loading text="加载中" /></td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={19} style={{padding:18, color:'var(--muted)'}}>暂无数据</td></tr>
            ) : rows.map((row) => (
              <tr key={row.id} style={{borderTop:'1px solid var(--border)'}}>
                <Td>{row.id}</Td>
                <Td>{getCategoryLabel(row.category)}</Td>
                <Td>{getTaskLabel(row.category, row.task_name)}</Td>
                <Td className="truncate" title={formatParamsSummary(row.params_json)}>{formatParamsSummary(row.params_json)}</Td>
                <Td>{row.workspace_id}</Td>
                <Td>{row.auth_id}</Td>
                <Td className="truncate" title={row.advertiser_id}>{row.advertiser_id}</Td>
                <Td className="truncate" title={row.store_id}>{row.store_id}</Td>
                <Td>{row.promotion_type || '-'}</Td>
                <Td>{row.level || '-'}</Td>
                <Td>{row.interval_minutes}</Td>
                <Td>{row.max_campaigns_per_run ?? '-'}</Td>
                <Td>
                  <label style={{display:'flex', alignItems:'center', gap:6}}>
                    <input
                      type="checkbox"
                      checked={!!row.enabled}
                      disabled={toggleMutation.isLoading}
                      onChange={(e) => toggleMutation.mutate({ id: row.id, enabled: e.target.checked })}
                    />
                    <span>{row.enabled ? '启用' : '停用'}</span>
                  </label>
                </Td>
                <Td>{formatDate(row.last_run_at)}</Td>
                <Td>{formatDate(row.last_success_at)}</Td>
                <Td>{formatDate(row.last_error_at)}</Td>
                <Td className="truncate" title={row.last_error || ''}>{row.last_error || '-'}</Td>
                <Td>{formatDate(row.updated_at)}</Td>
                <Td>
                  <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
                    <button className="btn small" onClick={() => setEditing(row)}>编辑</button>
                    <button
                      className="btn small ghost"
                      onClick={() => {
                        if (window.confirm('确定要删除该策略吗？')) {
                          deleteMutation.mutate(row.id)
                        }
                      }}
                      disabled={deleteMutation.isLoading}
                    >
                      删除
                    </button>
                  </div>
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{marginTop:12, color:'var(--muted)'}}>共 {total} 条记录</div>

      <StrategyFormModal
        open={showForm}
        mode="create"
        onClose={() => setShowForm(false)}
        onSubmit={handleSubmit}
      />

      <StrategyFormModal
        open={!!editing}
        mode="edit"
        initial={editing}
        onClose={() => setEditing(null)}
        onSubmit={handleSubmit}
      />
    </div>
  )
}

function Th({ children, style }) {
  return <th style={{textAlign:'left', padding:'10px 12px', fontWeight:700, ...style}}>{children}</th>
}

function Td({ children, className, title }) {
  return (
    <td
      className={className}
      title={title}
      style={{ padding:'10px 12px', maxWidth:260, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}
    >
      {children}
    </td>
  )
}
