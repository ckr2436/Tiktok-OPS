// src/features/platform/kie_ai/pages/PlatformKieKeyPage.jsx
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import kiePlatformApi from '../service.js'
import Modal from '../../../../components/ui/Modal.jsx'
import FormField from '../../../../components/ui/FormField.jsx'
import Loading from '../../../../components/ui/Loading.jsx'

const CREDIT_CACHE_KEY = 'kie_platform_key_credit_cache'

function emptyForm() {
  return {
    name: '',
    api_key: '',
    is_default: false,
  }
}

function loadCreditCache() {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(CREDIT_CACHE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function saveCreditCache(map) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(CREDIT_CACHE_KEY, JSON.stringify(map || {}))
  } catch {
    // ignore
  }
}

export default function PlatformKieKeyPage() {
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(emptyForm())
  const [saveError, setSaveError] = useState('')
  const [creditMap, setCreditMap] = useState(() => loadCreditCache())

  const queryClient = useQueryClient()

  const keysQuery = useQuery({
    queryKey: ['platform-kie-keys'],
    queryFn: kiePlatformApi.listKeys,
  })

  const defaultCreditQuery = useQuery({
    queryKey: ['platform-kie-default-credit'],
    queryFn: kiePlatformApi.getDefaultKeyCredit,
  })

  const items = Array.isArray(keysQuery.data) ? keysQuery.data : []
  const loading = keysQuery.isLoading || keysQuery.isFetching
  const errorMessage = keysQuery.error instanceof Error
    ? keysQuery.error.message
    : (keysQuery.error?.message || '')
  const defaultCredit = defaultCreditQuery.data ?? null

  useEffect(() => {
    const cache = loadCreditCache()
    const next = {}
    for (const it of items) {
      if (cache[it.id] != null) {
        next[it.id] = cache[it.id]
      }
    }
    setCreditMap(next)
  }, [items])

  const saveKeyMutation = useMutation({
    mutationFn: ({ mode, id, payload }) => (
      mode === 'create'
        ? kiePlatformApi.createKey(payload)
        : kiePlatformApi.updateKey(id, payload)
    ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['platform-kie-keys'] })
      queryClient.invalidateQueries({ queryKey: ['platform-kie-default-credit'] })
    },
  })

  const deactivateMutation = useMutation({
    mutationFn: kiePlatformApi.deactivateKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['platform-kie-keys'] })
      queryClient.invalidateQueries({ queryKey: ['platform-kie-default-credit'] })
    },
  })

  const updateKeyMutation = useMutation({
    mutationFn: ({ id, payload }) => kiePlatformApi.updateKey(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['platform-kie-keys'] })
      queryClient.invalidateQueries({ queryKey: ['platform-kie-default-credit'] })
    },
  })

  const creditMutation = useMutation({
    mutationFn: (id) => kiePlatformApi.getKeyCredit(id),
  })

  const openCreate = () => {
    setEditing(null)
    setForm(emptyForm())
    setSaveError('')
    setModalOpen(true)
  }

  const openEdit = (item) => {
    setEditing(item)
    setForm({
      name: item.name || '',
      api_key: '',
      is_default: !!item.is_default,
    })
    setSaveError('')
    setModalOpen(true)
  }

  const closeModal = () => {
    if (saveKeyMutation.isPending) return
    setModalOpen(false)
  }

  const onChange = (field, value) => {
    setForm((f) => ({ ...f, [field]: value }))
  }

  const handleSubmit = async (event) => {
    event.preventDefault()
    if (saveKeyMutation.isPending) return
    setSaveError('')

    try {
      if (!form.name.trim()) {
        throw new Error('名称不能为空')
      }

      const payload = {
        name: form.name.trim(),
        is_default: !!form.is_default,
      }

      const apiKey = form.api_key.trim()
      if (!editing) {
        if (!apiKey) throw new Error('API Key 不能为空')
        payload.api_key = apiKey
      } else if (apiKey) {
        payload.api_key = apiKey
      }

      await saveKeyMutation.mutateAsync({
        mode: editing ? 'update' : 'create',
        id: editing?.id,
        payload,
      })
      setModalOpen(false)
    } catch (err) {
      setSaveError(err?.message || '保存失败')
    }
  }

  const refreshDefaultCredit = () => {
    defaultCreditQuery.refetch()
  }

  const handleDeactivate = async (item) => {
    if (!window.confirm(`确定要停用「${item.name}」吗？`)) return
    try {
      await deactivateMutation.mutateAsync(item.id)
    } catch (err) {
      window.alert(err?.message || '停用失败')
    }
  }

  const handleActivate = async (item) => {
    try {
      await updateKeyMutation.mutateAsync({ id: item.id, payload: { is_active: true } })
    } catch (err) {
      window.alert(err?.message || '启用失败')
    }
  }

  const handleSetDefault = async (item) => {
    try {
      await updateKeyMutation.mutateAsync({ id: item.id, payload: { is_default: true } })
    } catch (err) {
      window.alert(err?.message || '设置默认失败')
    }
  }

  const handleCheckCredit = async (item) => {
    try {
      const v = await creditMutation.mutateAsync(item.id)
      setCreditMap((prev) => {
        const next = { ...prev, [item.id]: v }
        saveCreditCache(next)
        return next
      })
      if (item.is_default) {
        queryClient.setQueryData(['platform-kie-default-credit'], v)
      }
    } catch (err) {
      window.alert(err?.message || '查询余额失败')
    }
  }

  const totalActive = useMemo(
    () => (items || []).filter((x) => x.is_active).length,
    [items],
  )

  const isUpdatingId = updateKeyMutation.isPending ? updateKeyMutation.variables?.id : null
  const isDeactivatingId = deactivateMutation.isPending ? deactivateMutation.variables : null
  const isCheckingId = creditMutation.isPending ? creditMutation.variables : null

  return (
    <div>
      <header className="page-header">
        <h1 className="page-title">KIE AI - 平台 API Key 管理</h1>
        <div className="page-header__extra">
          <button type="button" className="btn btn--primary" onClick={openCreate}>
            新建 Key
          </button>
        </div>
      </header>

      {defaultCredit != null && (
        <div
          className="alert alert--info"
          style={{ marginBottom: '16px', display: 'flex', alignItems: 'center', gap: 12 }}
        >
          <span>
            默认 Key 当前余额：<strong>{defaultCredit}</strong> credits
          </span>
          <button
            type="button"
            className="btn btn--sm"
            onClick={refreshDefaultCredit}
            disabled={defaultCreditQuery.isFetching}
          >
            {defaultCreditQuery.isFetching ? '刷新中…' : '刷新'}
          </button>
        </div>
      )}

      {errorMessage && (
        <div className="alert alert--error" style={{ marginBottom: '16px' }}>
          {errorMessage}
        </div>
      )}

      {loading ? (
        <Loading />
      ) : (
        <div className="card">
          <div className="card__header">
            <div>共 {items.length} 个 Key，其中启用 {totalActive} 个</div>
          </div>
          <div className="card__body">
            {items.length === 0 ? (
              <div className="empty">暂无数据，请先创建一个 KIE API Key</div>
            ) : (
              <div className="table-wrapper">
                <table className="table">
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>名称</th>
                      <th>Provider</th>
                      <th>状态</th>
                      <th>默认</th>
                      <th>余额</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((it) => (
                      <tr key={it.id}>
                        <td>{it.id}</td>
                        <td>{it.name}</td>
                        <td>{it.provider_key}</td>
                        <td>{it.is_active ? '启用' : '停用'}</td>
                        <td>{it.is_default ? '是' : '否'}</td>
                        <td>
                          {creditMap[it.id] != null ? (
                            <span style={{ marginRight: 8 }}>{creditMap[it.id]}</span>
                          ) : (
                            <span className="small-muted" style={{ marginRight: 8 }}>
                              -
                            </span>
                          )}
                          <button
                            type="button"
                            className="btn btn--sm"
                            onClick={() => handleCheckCredit(it)}
                            disabled={isCheckingId === it.id}
                          >
                            {isCheckingId === it.id
                              ? '查询中…'
                              : (creditMap[it.id] != null ? '刷新余额' : '查询余额')}
                          </button>
                        </td>
                        <td>
                          <button
                            type="button"
                            className="btn btn--sm"
                            onClick={() => openEdit(it)}
                          >
                            编辑
                          </button>

                          {it.is_active ? (
                            <button
                              type="button"
                              className="btn btn--sm btn--danger"
                              style={{ marginLeft: 8 }}
                              onClick={() => handleDeactivate(it)}
                              disabled={isDeactivatingId === it.id}
                            >
                              {isDeactivatingId === it.id ? '停用中…' : '停用'}
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="btn btn--sm"
                              style={{ marginLeft: 8 }}
                              onClick={() => handleActivate(it)}
                              disabled={isUpdatingId === it.id}
                            >
                              {isUpdatingId === it.id ? '启用中…' : '启用'}
                            </button>
                          )}

                          {it.is_active && !it.is_default && (
                            <button
                              type="button"
                              className="btn btn--sm"
                              style={{ marginLeft: 8 }}
                              onClick={() => handleSetDefault(it)}
                              disabled={isUpdatingId === it.id}
                            >
                              {isUpdatingId === it.id ? '设置中…' : '设为默认'}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      <Modal open={modalOpen} title={editing ? '编辑 Key' : '新建 Key'} onClose={closeModal}>
        <form onSubmit={handleSubmit} className="form vertical">
          <FormField label="名称">
            <input
              type="text"
              className="input"
              value={form.name}
              onChange={(e) => onChange('name', e.target.value)}
              maxLength={128}
              required
            />
          </FormField>

          <FormField label="API Key">
            <input
              type="text"
              className="input"
              value={form.api_key}
              onChange={(e) => onChange('api_key', e.target.value)}
              placeholder={editing ? '留空则不修改' : ''}
            />
          </FormField>

          <FormField label="是否设为默认 Key">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={!!form.is_default}
                onChange={(e) => onChange('is_default', e.target.checked)}
              />
              <span>默认使用此 Key</span>
            </label>
          </FormField>

          {saveError && (
            <div className="form__error" style={{ marginBottom: 8 }}>
              {saveError}
            </div>
          )}

          <div className="form__actions">
            <button
              type="button"
              className="btn"
              onClick={closeModal}
              disabled={saveKeyMutation.isPending}
            >
              取消
            </button>
            <button
              type="submit"
              className="btn btn--primary"
              disabled={saveKeyMutation.isPending}
              style={{ marginLeft: 8 }}
            >
              {saveKeyMutation.isPending ? '保存中...' : '保存'}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  )
}
