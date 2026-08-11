// src/components/layout/Header.jsx
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import ThemeToggle from '../ui/ThemeToggle.jsx';
import ApiHealthBadge from '../ui/ApiHealthBadge.jsx';
import { useAppDispatch, useAppSelector } from '../../app/hooks.js';
import auth from '../../features/platform/auth/service.js';
import { clearSession } from '../../features/platform/auth/sessionSlice.js';

/** 用户菜单（内置，避免外部依赖） */
function UserMenu() {
  const me = useAppSelector((s) => s.session?.data);
  const dispatch = useAppDispatch();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  const name = useMemo(() => {
    return me?.display_name || me?.username || (me?.email ? me.email.split('@')[0] : '用户');
  }, [me]);
  const email = me?.email || '';
  const avatarText = (name || 'G').trim().charAt(0).toUpperCase();

  // 点击空白关闭 / Esc 关闭
  useEffect(() => {
    function onDocClick(e) {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(e.target)) setOpen(false);
    }
    function onKey(e) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('click', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('click', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, []);

  async function doLogout() {
    try {
      await auth.logout();
    } finally {
      dispatch(clearSession());
      queryClient.clear();
      localStorage.setItem('gmv.remember', '0');
      window.location.replace('/login?logged_out=1');
    }
  }

  return (
    <div ref={rootRef} className={`user-menu${open ? ' open' : ''}`}>
      <button
        type="button"
        className="user-btn"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open ? 'true' : 'false'}
        title={`${name}（点击展开）`}
      >
        <span className="avatar" aria-hidden>{avatarText}</span>
        <span className="user-email">{name}</span>
        <svg className="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      <div className="menu" role="menu">
        <div className="menu-header">
          <span className="avatar lg" aria-hidden>{avatarText}</span>
          <div>
            <div className="email">{name}</div>
            {email ? <div className="small-muted">{email}</div> : null}
          </div>
        </div>

        {/* 预留“个人资料”等入口 */}
        {/* <button className="menu-item" role="menuitem">个人资料</button> */}

        <div className="menu-sep" />

        <button className="menu-item danger" role="menuitem" onClick={doLogout}>
          退出登录
        </button>
      </div>
    </div>
  );
}

export default function Header({ mobileNavigationOpen = false, onToggleMobileNavigation }) {
  return (
    <header className="topbar" role="banner">
      <div className="topbar__leading">
        <button
          type="button"
          className="mobile-nav-toggle"
          aria-label={mobileNavigationOpen ? '关闭导航' : '打开导航'}
          aria-expanded={mobileNavigationOpen}
          aria-controls="app-sidebar"
          onClick={onToggleMobileNavigation}
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
            {mobileNavigationOpen ? (
              <>
                <path d="M6 6l12 12" />
                <path d="M18 6L6 18" />
              </>
            ) : (
              <>
                <path d="M4 6h16" />
                <path d="M4 12h16" />
                <path d="M4 18h16" />
              </>
            )}
          </svg>
        </button>
        <Link to="/dashboard" className="brand" aria-label="应用品牌：GMV Ops">
          <span className="logo" aria-hidden>G</span>
          <span className="brand-name">GMV Ops</span>
        </Link>
      </div>

      <div className="top-actions">
        <ApiHealthBadge />
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}
