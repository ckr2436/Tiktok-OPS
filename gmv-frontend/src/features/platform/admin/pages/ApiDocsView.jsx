// src/features/platform/admin/pages/ApiDocsView.jsx
import { useEffect, useMemo, useState } from 'react'

export default function ApiDocsView(){
  const [view, setView] = useState('swagger') // 'swagger' | 'redoc'
  const src = useMemo(
    () => (view === 'swagger' ? '/api/admin-docs/docs' : '/api/admin-docs/redoc'),
    [view]
  )

  // 让主内容区不滚动，仅 iframe 滚动，避免出现两个滚动条
  useEffect(() => {
    const content = document.querySelector('main.content')
    if (content) content.classList.add('no-scroll')
    return () => { if (content) content.classList.remove('no-scroll') }
  }, [])

  return (
    <div
      className="card card--elevated api-page"
      style={{
        padding:'10px 12px',
        height:'100%',             // 占满 content 高度
        display:'flex',
        flexDirection:'column',
        minHeight:0                // 允许子元素按 flex 约束收缩
      }}
    >
      <div style={{display:'flex', alignItems:'center', justifyContent:'space-between', gap:12, marginBottom:8}}>
        <h3 style={{margin:0}}>API 文档</h3>
        <div className="api-toolbar">
          <button
            className="btn ghost"
            onClick={()=>setView('swagger')}
            aria-pressed={view === 'swagger'}
            title="Swagger UI"
          >
            Swagger
          </button>
          <button
            className="btn ghost"
            onClick={()=>setView('redoc')}
            aria-pressed={view === 'redoc'}
            title="ReDoc"
          >
            ReDoc
          </button>
          <a
            className="btn ghost"
            href="/api/admin-docs/redoc#tag/Platform"
            target="_blank"
            rel="noopener"
            title="跳转至平台策略文档"
          >
            平台策略 API
          </a>
          <a
            className="btn"
            href="/api/admin-docs/openapi.json"
            target="_blank"
            rel="noopener"
            title="查看 OpenAPI JSON"
          >
            原始定义
          </a>
        </div>
      </div>

      {/* iframe 成为唯一滚动容器：flex:1 + minHeight:0 */}
      <iframe
        title="admin-api-docs"
        src={src}
        className="api-iframe"
        style={{
          flex:1,
          minHeight:0,
          width:'100%',
          border:'1px solid var(--border)',
          borderRadius:'12px',
          background:'var(--panel)'
        }}
      />

      <div className="small-muted" style={{marginTop:8}}>
        仅平台管理员可访问；鉴权由后端控制。如遇 401/403，请使用平台管理员账号登录。
      </div>
    </div>
  )
}

