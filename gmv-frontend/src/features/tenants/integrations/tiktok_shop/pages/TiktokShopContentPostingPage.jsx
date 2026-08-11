import { useEffect, useMemo, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'

import {
  addTikTokShopCreatorShowcaseProducts,
  createTikTokShopContentPost,
  createTikTokShopCreatorAuthorization,
  getTikTokShopCreatorProducts,
  getTikTokShopCreatorProfile,
  getTikTokShopReadiness,
  listTikTokShopContentPosts,
  listTikTokShopCreatorAccounts,
  publishTikTokShopContentPost,
  refreshTikTokShopContentPost,
} from '../service.js'
import './contentPosting.css'


const ACTIVE_STATES = new Set(['QUEUED', 'UPLOADING', 'PRECHECKING', 'PUBLISHING', 'PROCESSING'])
const RETRYABLE_STATES = new Set(['QUEUE_FAILED', 'UPLOAD_FAILED', 'PRECHECK_TIMEOUT', 'STATUS_UNKNOWN', 'FAILED'])
const ACCEPTED_VIDEO_TYPES = '.mp4,.mov,.mpeg,.3gp,.avi,.webm,.mkv,.flv,.wmv'


export function contentPostingStatusText(value) {
  return ({
    QUEUED: '已排队',
    UPLOADING: '正在上传至 TikTok',
    PRECHECKING: '官方预检中',
    READY_TO_PUBLISH: '预检通过，待发布',
    PUBLISHING: '正在提交发布',
    PROCESSING: 'TikTok 发布处理中',
    SUCCESS: '发布成功',
    PRECHECK_FAILED: '预检未通过',
    QUEUE_FAILED: '排队失败',
    UPLOAD_FAILED: '上传失败',
    PRECHECK_TIMEOUT: '预检等待超时',
    STATUS_UNKNOWN: '发布状态等待超时',
    PUBLISH_UNCERTAIN: '发布结果待人工核对',
    FAILED: '发布失败',
  })[String(value || '').toUpperCase()] || String(value || '未知状态')
}


export function normalizeProductAnchorTitle(value) {
  return Array.from(String(value || ''))
    .filter((char) => /[\p{L}\p{N}\s]/u.test(char))
    .join('')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 30)
}


function newIdempotencyKey() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `tt-post-${crypto.randomUUID()}`
  }
  return `tt-post-${Date.now()}-${Math.random().toString(36).slice(2)}`
}


function errorText(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  return error?.message || '请求失败，请稍后重试。'
}


function formatDate(value) {
  if (!value) return '—'
  const date = new Date(String(value).endsWith('Z') ? value : `${value}Z`)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date)
}


function productImage(product) {
  return product?.images?.find((item) => item?.url)?.url || ''
}


export default function TiktokShopContentPostingPage() {
  const { wid } = useParams()
  const queryClient = useQueryClient()
  const [accountId, setAccountId] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [searchKeyword, setSearchKeyword] = useState('')
  const [selectedProduct, setSelectedProduct] = useState(null)
  const [videoFile, setVideoFile] = useState(null)
  const [videoTitle, setVideoTitle] = useState('')
  const [anchorTitle, setAnchorTitle] = useState('')
  const [coverTimestamp, setCoverTimestamp] = useState('1000')
  const [notice, setNotice] = useState(null)
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey)

  const readinessQuery = useQuery({
    queryKey: ['tiktok-shop-readiness', wid],
    queryFn: () => getTikTokShopReadiness(wid),
    enabled: Boolean(wid),
    staleTime: 5 * 60 * 1000,
  })
  const accountsQuery = useQuery({
    queryKey: ['tiktok-shop-creator-accounts', wid],
    queryFn: () => listTikTokShopCreatorAccounts(wid),
    enabled: Boolean(wid),
    refetchInterval: 60 * 1000,
  })
  const accounts = accountsQuery.data || []

  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(String(accounts[0].id))
  }, [accountId, accounts])

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (!params.get('shop_oauth')) return
    if (params.get('shop_oauth') === 'success' && params.get('account_type') === 'creator') {
      setNotice({ type: 'success', text: '达人授权成功。现在可以读取达人商品并创建发布任务。' })
      queryClient.invalidateQueries({ queryKey: ['tiktok-shop-creator-accounts', wid] })
    } else if (params.get('shop_oauth') === 'error') {
      setNotice({ type: 'error', text: '达人授权未完成，请重新授权并确认 creator.video.write 权限。' })
    }
    window.history.replaceState({}, '', window.location.pathname)
  }, [queryClient, wid])

  const selectedAccount = accounts.find((item) => String(item.id) === String(accountId))
  const canUseAccount = Boolean(selectedAccount?.content_posting_ready)
  const profileQuery = useQuery({
    queryKey: ['tiktok-shop-creator-profile', wid, accountId],
    queryFn: () => getTikTokShopCreatorProfile(wid, accountId),
    enabled: Boolean(wid && accountId && canUseAccount),
    staleTime: 5 * 60 * 1000,
  })
  const productsQuery = useInfiniteQuery({
    queryKey: ['tiktok-shop-creator-products', wid, accountId, searchKeyword],
    queryFn: ({ pageParam }) => getTikTokShopCreatorProducts(wid, accountId, {
      title_keyword: searchKeyword || undefined,
      sort_field: 'SALE',
      sort_order: 'DESC',
      page_size: 100,
      page_token: pageParam || undefined,
    }),
    initialPageParam: '',
    getNextPageParam: (lastPage) => lastPage?.data?.next_page_token || undefined,
    enabled: Boolean(wid && accountId && canUseAccount),
  })
  const products = useMemo(() => {
    const unique = new Map()
    for (const page of productsQuery.data?.pages || []) {
      for (const product of page?.data?.products || []) {
        if (product?.id && !unique.has(product.id)) unique.set(product.id, product)
      }
    }
    return [...unique.values()]
  }, [productsQuery.data])
  const productTotal = Number(productsQuery.data?.pages?.[0]?.data?.total_count || products.length)

  const postsQuery = useQuery({
    queryKey: ['tiktok-shop-content-posts', wid, accountId],
    queryFn: () => listTikTokShopContentPosts(wid, {
      account_id: accountId || undefined,
      page: 1,
      page_size: 50,
    }),
    enabled: Boolean(wid && accountId),
    refetchInterval: (query) => {
      const items = query.state.data?.items || []
      return items.some((item) => ACTIVE_STATES.has(item.workflow_status)) ? 10000 : 30000
    },
  })

  const creatorConnectMutation = useMutation({
    mutationFn: () => createTikTokShopCreatorAuthorization(wid, {
      provider_app_id: readinessQuery.data?.provider_app_id || null,
      alias: 'TikTok 达人账号',
      return_to: `/tenants/${encodeURIComponent(wid)}/tiktok-shop/content-posting`,
    }),
    onSuccess: ({ auth_url: authUrl }) => window.location.assign(authUrl),
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })
  const showcaseMutation = useMutation({
    mutationFn: (productId) => addTikTokShopCreatorShowcaseProducts(wid, accountId, [productId]),
    onSuccess: () => {
      productsQuery.refetch()
      setNotice({ type: 'success', text: '商品已提交加入达人橱窗。' })
    },
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })
  const createMutation = useMutation({
    mutationFn: () => createTikTokShopContentPost(wid, {
      accountId,
      productId: selectedProduct.id,
      productLinkTitle: anchorTitle.trim(),
      video: videoFile,
      videoTitle: videoTitle.trim(),
      coverTimestampMs: coverTimestamp === '' ? null : Number(coverTimestamp),
      idempotencyKey,
    }),
    onSuccess: async ({ item, reused }) => {
      await queryClient.invalidateQueries({ queryKey: ['tiktok-shop-content-posts', wid, accountId] })
      setNotice({
        type: 'success',
        text: reused ? `已返回原任务 #${item.id}，没有重复上传。` : `任务 #${item.id} 已创建，正在上传并执行官方预检。`,
      })
      if (!reused) {
        setVideoFile(null)
        setVideoTitle('')
        setIdempotencyKey(newIdempotencyKey())
        const input = document.getElementById('content-posting-video')
        if (input) input.value = ''
      }
    },
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })
  const publishMutation = useMutation({
    mutationFn: (postId) => publishTikTokShopContentPost(wid, postId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['tiktok-shop-content-posts', wid, accountId] })
      setNotice({ type: 'success', text: '已提交正式发布，正在等待 TikTok 返回最终状态。' })
    },
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })
  const refreshMutation = useMutation({
    mutationFn: (postId) => refreshTikTokShopContentPost(wid, postId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tiktok-shop-content-posts', wid, accountId] }),
    onError: (error) => setNotice({ type: 'error', text: errorText(error) }),
  })

  const profile = profileQuery.data?.data || null
  const posts = postsQuery.data?.items || []
  const anchorInvalid = !anchorTitle.trim()
    || Array.from(anchorTitle.trim()).length > 30
    || /[^\p{L}\p{N}\s]/u.test(anchorTitle)
  const fileTooLarge = Number(videoFile?.size || 0) > 100 * 1024 * 1024
  const canCreate = canUseAccount && selectedProduct && videoFile && !fileTooLarge && !anchorInvalid
  const summary = useMemo(() => ({
    pending: posts.filter((item) => ACTIVE_STATES.has(item.workflow_status)).length,
    ready: posts.filter((item) => item.workflow_status === 'READY_TO_PUBLISH').length,
    success: posts.filter((item) => item.workflow_status === 'SUCCESS').length,
  }), [posts])

  const selectProduct = (product) => {
    setSelectedProduct(product)
    setAnchorTitle(normalizeProductAnchorTitle(product.title) || '查看商品')
  }

  return (
    <main className="content-posting-page">
      <header className="content-posting-hero">
        <div>
          <div className="content-posting-eyebrow">TikTok Shop · Content Posting API</div>
          <h1>视频发布工作台</h1>
          <p>选择达人商品、上传本地视频，先经过 TikTok 官方预检，再由你确认正式发布。</p>
        </div>
        <div className="content-posting-actions">
          <Link className="btn ghost" to={`/tenants/${wid}/tiktok-shop`}>授权管理</Link>
          <Link className="btn ghost" to={`/tenants/${wid}/tiktok-shop/videos`}>发布后分析</Link>
          <button
            className="btn"
            disabled={!readinessQuery.data?.configured || creatorConnectMutation.isPending}
            onClick={() => creatorConnectMutation.mutate()}
          >
            {creatorConnectMutation.isPending ? '正在跳转…' : accounts.length ? '重新授权达人' : '授权达人账号'}
          </button>
        </div>
      </header>

      {notice && <div className={`alert alert--${notice.type}`}>{notice.text}</div>}
      {!accountsQuery.isLoading && accounts.length === 0 && (
        <section className="content-posting-empty">
          <strong>尚无达人授权</strong>
          <p>现有 Seller 授权不能发布视频。请使用 TikTok 达人授权流程，并授予 creator.video.write。</p>
        </section>
      )}

      <section className="content-posting-summary">
        <Summary label="进行中" value={summary.pending} />
        <Summary label="待确认发布" value={summary.ready} />
        <Summary label="发布成功" value={summary.success} />
        <Summary label="官方上限" value="100 MB" />
      </section>

      <div className="content-posting-grid">
        <section className="content-posting-panel">
          <div className="content-posting-panel__heading">
            <div><span>1</span><div><h2>达人账号与商品</h2><p>商品来自 Creator Shop Products 官方接口。</p></div></div>
          </div>
          <label className="content-posting-field">
            <span>达人账号</span>
            <select value={accountId} onChange={(event) => { setAccountId(event.target.value); setSelectedProduct(null) }}>
              <option value="">请选择达人账号</option>
              {accounts.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.alias || item.seller_name || `达人 #${item.id}`}{item.content_posting_ready ? '' : '（需重新授权）'}
                </option>
              ))}
            </select>
          </label>
          {profile && (
            <div className="creator-profile">
              {profile.avatar?.url && <img src={profile.avatar.url} alt="达人头像" />}
              <div><strong>@{profile.username || 'TikTok Creator'}</strong><span>{profile.selection_region || profile.register_region || '—'} · {profile.user_type || 'CREATOR'}</span></div>
            </div>
          )}
          {selectedAccount && !canUseAccount && (
            <div className="content-posting-warning">该授权缺少 creator.video.write 或已失效，请重新授权。</div>
          )}
          <form className="product-search" onSubmit={(event) => { event.preventDefault(); setSearchKeyword(searchInput.trim()) }}>
            <input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="搜索达人绑定店铺的商品" />
            <button className="btn ghost" type="submit" disabled={!canUseAccount}>搜索</button>
          </form>
          <div className="creator-products">
            {productsQuery.isLoading && <div className="small-muted">正在读取达人商品…</div>}
            {!productsQuery.isLoading && canUseAccount && products.length === 0 && <div className="small-muted">没有找到可推广商品。</div>}
            {products.map((product) => (
              <article key={product.id} className={`creator-product ${selectedProduct?.id === product.id ? 'is-selected' : ''}`}>
                <button type="button" className="creator-product__select" onClick={() => selectProduct(product)}>
                  {productImage(product) ? <img src={productImage(product)} alt="" /> : <span className="creator-product__placeholder">商品</span>}
                  <span><strong>{product.title}</strong><small>{product.price?.currency} {product.price?.amount} · 已售 {product.sales_count || 0}</small></span>
                </button>
                <div className="creator-product__footer">
                  <span className={`product-state product-state--${String(product.added_status || '').toLowerCase()}`}>{product.added_status || '未知状态'}</span>
                  {product.added_status === 'ADDABLE' && <button type="button" onClick={() => showcaseMutation.mutate(product.id)} disabled={showcaseMutation.isPending}>加入橱窗</button>}
                </div>
              </article>
            ))}
          </div>
          {products.length > 0 && (
            <div className="product-pagination">
              <span>已加载 {products.length} / {productTotal}</span>
              {productsQuery.hasNextPage && (
                <button className="btn ghost" onClick={() => productsQuery.fetchNextPage()} disabled={productsQuery.isFetchingNextPage}>
                  {productsQuery.isFetchingNextPage ? '加载中…' : '加载更多商品'}
                </button>
              )}
            </div>
          )}
        </section>

        <section className="content-posting-panel">
          <div className="content-posting-panel__heading">
            <div><span>2</span><div><h2>视频与商品锚点</h2><p>创建任务只会上传并预检，不会立即公开发布。</p></div></div>
          </div>
          {selectedProduct && (
            <div className="selected-product"><strong>已选商品</strong><span>{selectedProduct.title}</span><small>ID {selectedProduct.id}</small></div>
          )}
          <label className="content-posting-field">
            <span>视频文件</span>
            <input id="content-posting-video" type="file" accept={ACCEPTED_VIDEO_TYPES} onChange={(event) => { setVideoFile(event.target.files?.[0] || null); setIdempotencyKey(newIdempotencyKey()) }} />
            <small>MP4 / MOV / WEBM 等官方支持格式，最大 100 MB。</small>
            {fileTooLarge && <em>文件超过 100 MB，无法上传。</em>}
          </label>
          <label className="content-posting-field">
            <span>商品锚点标题 <b>{Array.from(anchorTitle).length}/30</b></span>
            <input value={anchorTitle} maxLength={30} onChange={(event) => setAnchorTitle(event.target.value)} placeholder="例如 Sleep Ease Gummies" />
            <small>官方不允许标点、符号或表情，只可使用文字、数字和空格。</small>
            {anchorInvalid && anchorTitle && <em>请移除标点、符号或表情，并控制在 30 字以内。</em>}
          </label>
          <label className="content-posting-field">
            <span>视频文案 <b>{videoTitle.length}/2200</b></span>
            <textarea value={videoTitle} maxLength={2200} rows={6} onChange={(event) => setVideoTitle(event.target.value)} placeholder="输入发布文案和合规标签" />
          </label>
          <label className="content-posting-field">
            <span>封面时间点（毫秒）</span>
            <input type="number" min="0" step="100" value={coverTimestamp} onChange={(event) => setCoverTimestamp(event.target.value)} />
            <small>默认取第 1 秒画面；后端不传临时封面地址。</small>
          </label>
          <button className="btn content-posting-submit" disabled={!canCreate || createMutation.isPending} onClick={() => createMutation.mutate()}>
            {createMutation.isPending ? '正在安全上传…' : '上传并执行官方预检'}
          </button>
        </section>
      </div>

      <section className="content-posting-panel content-posting-workflows">
        <div className="content-posting-panel__heading">
          <div><span>3</span><div><h2>发布任务</h2><p>进行中任务每 10 秒刷新；官方 request_id 已保留用于排障。</p></div></div>
          <button className="btn ghost" onClick={() => postsQuery.refetch()} disabled={postsQuery.isFetching}>{postsQuery.isFetching ? '刷新中…' : '刷新状态'}</button>
        </div>
        {postsQuery.isLoading ? <div className="small-muted">正在读取发布任务…</div> : posts.length === 0 ? <div className="content-posting-note">还没有发布任务。</div> : (
          <div className="content-posting-table-wrap">
            <table className="content-posting-table">
              <thead><tr><th>任务 / 视频</th><th>商品</th><th>官方预检</th><th>发布状态</th><th>创建时间</th><th>操作</th></tr></thead>
              <tbody>{posts.map((post) => (
                <tr key={post.id}>
                  <td><strong>#{post.id} · {post.original_filename}</strong><small>{post.video_id ? `Video ID ${post.video_id}` : `${(post.file_size / 1024 / 1024).toFixed(1)} MB`}</small></td>
                  <td><strong>{post.product_link_title}</strong><small>{post.product_id}</small></td>
                  <td><Status
                    value={post.precheck_status || (post.precheck_task_id ? 'PROCESSING' : 'WAITING')}
                    label={({ SUCCESS: '预检通过', FAIL: '预检未通过', PROCESSING: '预检中' })[post.precheck_status]}
                  />{post.precheck_issues?.map((issue, index) => <small className="issue" key={`${post.id}-${index}`}>{issue.risk || issue.code || '预检问题'}：{issue.suggestions || '请修改视频后重试'}</small>)}</td>
                  <td><Status value={post.workflow_status} /><small>{post.post_status ? `TikTok: ${post.post_status}` : contentPostingStatusText(post.workflow_status)}</small>{post.last_error_message && <small className="issue">{post.last_error_message}</small>}</td>
                  <td>{formatDate(post.created_at)}{post.last_error_request_id && <small>Request ID {post.last_error_request_id}</small>}</td>
                  <td><div className="workflow-actions">
                    {post.workflow_status === 'READY_TO_PUBLISH' && !post.publish_requested && <button className="btn sm" disabled={publishMutation.isPending} onClick={() => window.confirm('官方预检已通过。确定现在正式发布到 TikTok 吗？') && publishMutation.mutate(post.id)}>确认发布</button>}
                    {RETRYABLE_STATES.has(post.workflow_status) && <button className="btn sm ghost" disabled={refreshMutation.isPending} onClick={() => refreshMutation.mutate(post.id)}>继续处理</button>}
                    {post.workflow_status === 'PUBLISH_UNCERTAIN' && <span className="manual-check">禁止自动重发，请先去 TikTok 核对</span>}
                  </div></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  )
}


function Summary({ label, value }) {
  return <div><span>{label}</span><strong>{value}</strong></div>
}


function Status({ value, label }) {
  const normalized = String(value || '').toUpperCase()
  const tone = ['SUCCESS', 'READY_TO_PUBLISH'].includes(normalized)
    ? 'success'
    : (normalized.includes('FAIL') || normalized.includes('UNCERTAIN') ? 'danger' : 'progress')
  return <span className={`workflow-status workflow-status--${tone}`}>{label || (normalized === 'WAITING' ? '等待中' : (normalized === 'PROCESSING' ? '处理中' : contentPostingStatusText(normalized)))}</span>
}
