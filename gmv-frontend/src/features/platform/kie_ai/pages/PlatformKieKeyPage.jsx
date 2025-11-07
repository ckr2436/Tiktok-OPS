// src/features/platform/kie_ai/pages/PlatformKieKeyPage.jsx
import { useEffect, useMemo, useState } from 'react'
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
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [defaultCredit, setDefaultCredit] = useState(null)

  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState(null) // null = 新建
  const [form, setForm] = useState(emptyForm())
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')

  // { keyId: credits }
  const [creditMap, setCreditMap] = useState(() => loadCreditCache())

  const refresh = async () => {
    setLoading(true)
    setError('')
    try {
      const list = await kiePlatformApi.listKeys()
      const arr = list || []
      setItems(arr)

      // 把本地缓存里已有的余额映射到当前 key 列表上
      const cache = loadCreditCache()
      const next = {}
      for (const it of arr) {
        if (cache[it.id] != null) {
          next[it.id] = cache[it.id]
        }
      }
      setCreditMap(next)
    } catch (err) {
      setError(err?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }

  const refreshDefaultCredit = async () => {
    try {
      const v = await kiePlatformApi.getDefaultKeyCredit()
      setDefaultCredit(v)
    } catch {
      // 查询失败就保持原值不动
    }
  }

  useEffect(() => {
    refresh()
    refreshDefaultCredit()
  }, [])

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
    if (saving) return
    setModalOpen(false)
  }

  const onChange = (field, value) => {
    setForm((f) => ({ ...f, [field]: value }))
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (saving) return
    setSaving(true)
    setSaveError('')

    try {
      if (!form.name.trim()) {
        throw new Error('名称不能为空')
      }

      // 编辑时 api_key 可以留空（表示不修改）
      if (!editing && !form.api_key.trim()) {
        throw new Error('API Key 不能为空')
      }

      if (editing) {
        const payload = {
          name: form.name.trim(),
          is_default: !!form.is_default,
        }
        if (form.api_key.trim()) {
          payload.api_key = form.api_key.trim()
        }
        await kiePlatformApi.updateKey(editing.id, payload)
      } else {
        await kiePlatformApi.createKey({
          name: form.name.trim(),
          api_key: form.api_key.trim(),
          is_default: !!form.is_default,
        })
      }
      await refresh()
      await refreshDefaultCredit()
      setModalOpen(false)
    } catch (err) {
      setSaveError(err?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleDeactivate = async (item) => {
    if (!window.confirm(`确定要停用「${item.name}」吗？`)) return
    try {
      await kiePlatformApi.deactivateKey(item.id)
      await refresh()
      await refreshDefaultCredit()
    } catch (err) {
      window.alert(err?.message || '停用失败')
    }
  }

  // 启用 key：走 PATCH，把 is_active 调回 true
  const handleActivate = async (item) => {
    try {
      await kiePlatformApi.updateKey(item.id, { is_active: true })
      await refresh()
      await refreshDefaultCredit()
    } catch (err) {
      window.alert(err?.message || '启用失败')
    }
  }

  // 设置为默认 key：走 PATCH is_default=true，后端会自动取消其他默认
  const handleSetDefault = async (item) => {
    try {
      await kiePlatformApi.updateKey(item.id, { is_default: true })
      await refresh()
      await refreshDefaultCredit()
    } catch (err) {
      window.alert(err?.message || '设置默认失败')
    }
  }

  const handleCheckCredit = async (item) => {
    try {
      const v = await kiePlatformApi.getKeyCredit(item.id)
      setCreditMap((m) => {
        const next = { ...m, [item.id]: v }
        saveCreditCache(next)
        return next
      })
      if (item.is_default) {
        setDefaultCredit(v)
      }
    } catch (err) {
      window.alert(err?.message || '查询余额失败')
    }
  }

  const totalActive = useMemo(
    () => (items || []).filter((x) => x.is_active).length,
    [items],
  )

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
          >
            刷新
          </button>
        </div>
      )}

      {error && (
        <div className="alert alert--error" style={{ marginBottom: '16px' }}>
          {error}
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
                          >
                            {creditMap[it.id] != null ? '刷新余额' : '查询余额'}
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
                            >
                              停用
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="btn btn--sm"
                              style={{ marginLeft: 8 }}
                              onClick={() => handleActivate(it)}
                            >
                              启用
                            </button>
                          )}

                          {it.is_active && !it.is_default && (
                            <button
                              type="button"
                              className="btn btn--sm"
                              style={{ marginLeft: 8 }}
                              onClick={() => handleSetDefault(it)}
                            >
                              设为默认
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
              disabled={saving}
            >
              取消
            </button>
            <button
              type="submit"
              className="btn btn--primary"
              disabled={saving}
              style={{ marginLeft: 8 }}
            >
              {saving ? '保存中...' : '保存'}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  )
}

