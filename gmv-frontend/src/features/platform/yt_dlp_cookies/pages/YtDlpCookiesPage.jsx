import { useCallback, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import CookiesTable from '../components/CookiesTable.jsx'
import LoginModal from '../components/LoginModal.jsx'
import { SITE_OPTIONS, deleteCookie, listCookies, updateCookieActivation } from '../api.js'

const ALL_SITE = 'all'

function useToast() {
  const [toast, setToast] = useState(null)

  const showToast = useCallback((message, tone = 'info', duration = 3000) => {
    setToast({ id: Date.now(), message, tone, duration })
  }, [])

  useEffect(() => {
    if (!toast) return undefined
    const timer = setTimeout(() => setToast(null), toast.duration)
    return () => clearTimeout(timer)
  }, [toast])

  return [toast, showToast]
}

function SiteTabs({ value, onChange }) {
  const options = useMemo(() => [{ value: ALL_SITE, label: '全部' }, ...SITE_OPTIONS], [])
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      {options.map((opt) => {
        const active = value === opt.value
        return (
          <button
            key={opt.value}
            className="btn ghost"
            onClick={() => onChange?.(opt.value)}
            style={{
              minWidth: 96,
              background: active ? 'var(--panel-2)' : undefined,
              borderColor: active ? 'var(--primary)' : undefined,
              color: active ? 'var(--primary)' : undefined,
            }}
          >
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}

export default function YtDlpCookiesPage() {
  const queryClient = useQueryClient()
  const [siteFilter, setSiteFilter] = useState(ALL_SITE)
  const [modalOpen, setModalOpen] = useState(false)
  const [prefill, setPrefill] = useState({ site: SITE_OPTIONS[0].value, label: '' })
  const [deletingId, setDeletingId] = useState('')
  const [toast, showToast] = useToast()

  const cookiesQuery = useQuery({
    queryKey: ['platform', 'yt-dlp', 'cookies', siteFilter],
    queryFn: () => listCookies(siteFilter === ALL_SITE ? undefined : siteFilter),
    staleTime: 10 * 1000,
  })
  const rows = cookiesQuery.data ?? []
  const loading = cookiesQuery.isLoading
  const errorMessage = cookiesQuery.error?.message || ''

  const toggleMutation = useMutation({
    mutationKey: ['platform', 'yt-dlp', 'cookies', 'toggle'],
    mutationFn: ({ id, isActive }) => updateCookieActivation(id, isActive),
  })

  const deleteMutation = useMutation({
    mutationKey: ['platform', 'yt-dlp', 'cookies', 'delete'],
    mutationFn: (id) => deleteCookie(id),
  })

  const refreshList = async () => {
    await queryClient.invalidateQueries({ queryKey: ['platform', 'yt-dlp', 'cookies'] })
  }

  const openModal = (site, label = '') => {
    setPrefill({ site: site || SITE_OPTIONS[0].value, label })
    setModalOpen(true)
  }

  const handleToggle = async (item, next) => {
    if (!item?.id) return
    try {
      await toggleMutation.mutateAsync({ id: item.id, isActive: !!next })
      await refreshList()
    } catch (err) {
      const msg = err?.message || '更新状态失败，请稍后再试。'
      window.alert(msg)
    }
  }

  const handleDelete = async (item) => {
    if (!item?.id || deletingId) return
    const ok = window.confirm(
      `确定删除「${item.label || item.site || item.id}」这条 Cookies 吗？\n删除后，系统将无法再使用这条 Cookies 下载对应平台的视频。`,
    )
    if (!ok) return
    setDeletingId(item.id)
    try {
      await deleteMutation.mutateAsync(item.id)
      await refreshList()
      showToast('Cookies 已删除', 'success')
    } catch (err) {
      const msg = err?.response?.data?.detail || err?.response?.data?.error_msg || err?.message || '删除失败，请稍后再试。'
      window.alert(typeof msg === 'string' ? msg : JSON.stringify(msg))
    } finally {
      setDeletingId('')
    }
  }

  const handleRefreshLogin = (item) => {
    openModal(item?.site || prefill.site, item?.label || '')
  }

  const handleCreate = () => {
    const site = siteFilter === ALL_SITE ? SITE_OPTIONS[0].value : siteFilter
    openModal(site, '')
  }

  return (
    <div className="card card--elevated" style={{ display: 'grid', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h2 style={{ margin: '0 0 6px 0' }}>yt-dlp Cookies 管理</h2>
          <div className="small-muted">
            用于手动保存 TikTok / 抖音 / YouTube 等站点的登录 Cookies，供系统内部下载非公开视频使用。仅平台管理员可操作，请使用公司授权账号。
          </div>
        </div>
        <button className="btn" onClick={handleCreate}>添加 / 抓取 Cookies</button>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <SiteTabs value={siteFilter} onChange={setSiteFilter} />
        {cookiesQuery.isFetching && <span className="small-muted">同步中…</span>}
      </div>

      {errorMessage && <div className="alert alert--error">{errorMessage}</div>}

      <CookiesTable
        items={rows}
        loading={loading}
        onToggle={handleToggle}
        onRefreshLogin={handleRefreshLogin}
        onDelete={handleDelete}
        deletingId={deletingId}
      />

      <LoginModal
        open={modalOpen}
        defaultSite={prefill.site}
        defaultLabel={prefill.label}
        onClose={() => setModalOpen(false)}
        onSuccess={refreshList}
        onToast={showToast}
      />

      {toast && (
        <div className={`toast toast--${toast.tone}`} role="status">
          {toast.message}
        </div>
      )}
    </div>
  )
}
