import { useEffect, useMemo, useState } from 'react';

const STORAGE_KEY = 'gmv.theme'; // 'light' | 'dark' | 'auto'

function getInitialTheme() {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'light' || saved === 'dark' || saved === 'auto') return saved;
  return 'auto';
}
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'auto') {
    root.removeAttribute('data-theme'); // 跟随系统
  } else {
    root.setAttribute('data-theme', theme);
  }
}

// 简单图标：太阳 / 月亮 / A（自动）
function SunIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12" />
    </svg>
  );
}
function MoonIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}
function AutoIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M4 12a8 8 0 0 1 16 0" />
      <path d="M12 20a8 8 0 0 1-8-8" />
      <text x="7" y="16" fontSize="8" fontFamily="system-ui, -apple-system">A</text>
    </svg>
  );
}

export default function ThemeToggle() {
  const [mode, setMode] = useState(getInitialTheme);

  // 系统主题改变时，如果是 auto 就实时跟随
  useEffect(() => {
    applyTheme(mode);
    localStorage.setItem(STORAGE_KEY, mode);
  }, [mode]);

  useEffect(() => {
    if (mode !== 'auto') return;
    const mql = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => applyTheme('auto');
    mql.addEventListener?.('change', onChange);
    return () => mql.removeEventListener?.('change', onChange);
  }, [mode]);

  const label = useMemo(() => {
    if (mode === 'dark') return '深色';
    if (mode === 'light') return '浅色';
    return '跟随系统';
  }, [mode]);

  const Icon = mode === 'dark' ? MoonIcon : mode === 'light' ? SunIcon : AutoIcon;

  // 简单下拉：点击切换到下一个模式（auto -> light -> dark -> auto）
  function cycle() {
    setMode((m) => (m === 'auto' ? 'light' : m === 'light' ? 'dark' : 'auto'));
  }

  return (
    <button
      type="button"
      className="theme-btn"
      onClick={cycle}
      aria-label={`外观：${label}`}
      title={`外观：${label}（点击切换）`}
    >
      <Icon />
      <span className="theme-btn__text">{label}</span>
    </button>
  );
}

