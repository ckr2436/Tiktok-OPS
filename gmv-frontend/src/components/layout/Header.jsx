// src/components/layout/Header.jsx
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import ThemeToggle from '../ui/ThemeToggle.jsx';
import { useAppSelector } from '../../app/hooks.js';
import auth from '../../features/platform/auth/service.js';

/** 用户菜单（内置，避免外部依赖） */
function UserMenu() {
  const me = useAppSelector((s) => s.session?.data);
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
    try { await auth.logout(); } finally { window.location.href = '/login'; }
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

export default function Header() {
  return (
    <header className="topbar" role="banner">
      <Link to="/dashboard" className="brand" aria-label="应用品牌：GMV Ops">
        <span className="logo" aria-hidden>G</span>
        <span className="brand-name">GMV Ops</span>
      </Link>

      <div className="top-actions">
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}

