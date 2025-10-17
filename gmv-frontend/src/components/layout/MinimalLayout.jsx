// src/components/layout/MinimalLayout.jsx
import { useEffect } from 'react'

/** 在登录/初始化页：强制跟随系统主题（忽略本地偏好） */
function applySystemTheme() {
  const mql = window.matchMedia('(prefers-color-scheme: dark)')
  const apply = () => {
    document.documentElement.dataset.theme = mql.matches ? 'dark' : 'light'
  }
  apply()
  // 监听系统主题变更，实时应用
  if (mql.addEventListener) {
    mql.addEventListener('change', apply)
    return () => mql.removeEventListener('change', apply)
  } else {
    // 兼容旧浏览器
    mql.addListener(apply)
    return () => mql.removeListener(apply)
  }
}

/**
 * 极简布局（用于登录 / 初始化等页）
 * @param {boolean} showDocs 是否显示“服务条款 / 隐私政策”的快捷按钮（默认 false）
 */
export default function MinimalLayout({ children, showDocs = false }) {
  useEffect(() => {
    const dispose = applySystemTheme()
    return dispose
  }, [])

  return (
    <div className="auth-page">
      <main className="min-layout__main">{children}</main>

      {/* 只有显式要求时才显示条款/隐私快捷按钮，默认隐藏 */}
      {showDocs && (
        <div className="doc-links" style={{ marginTop: 14, display: 'grid', gap: 8, justifyItems: 'center' }}>
          <div style={{ display: 'flex', gap: 10 }}>
            <a className="btn" href="/terms" style={{ padding: '6px 10px', fontSize: 12 }}>服务条款</a>
            <a className="btn" href="/privacy" style={{ padding: '6px 10px', fontSize: 12 }}>隐私政策</a>
          </div>
        </div>
      )}
    </div>
  )
}

