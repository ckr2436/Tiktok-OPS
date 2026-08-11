import { useEffect, useRef, useState } from 'react';

const RESET_DELAY = 1600;

export default function CopyButton({
  text,
  size = 'sm',
  className = '',
  onError,
  onSuccess,
}) {
  const [copied, setCopied] = useState(false);
  const [liveMessage, setLiveMessage] = useState('');
  const timerRef = useRef(null);

  const hasText = !(text === null || text === undefined || String(text) === '');

  useEffect(
    () => () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    },
    []
  );

  const scheduleReset = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => {
      setCopied(false);
      setLiveMessage('');
    }, RESET_DELAY);
  };

  const handleCopy = async () => {
    if (!hasText) return;
    const value = String(text);
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
      } else {
        const textarea = document.createElement('textarea');
        textarea.value = value;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
      }
      setCopied(true);
      setLiveMessage('内容已复制到剪贴板');
      onSuccess?.(value);
    } catch (err) {
      setCopied(false);
      setLiveMessage('复制失败');
      onError?.(err);
    } finally {
      scheduleReset();
    }
  };

  const classes = [`copy-btn`, `copy-btn--${size}`];
  if (className) classes.push(className);

  return (
    <button
      type="button"
      className={classes.join(' ')}
      onClick={handleCopy}
      disabled={!hasText}
      title={hasText ? '复制到剪贴板' : '无可复制内容'}
    >
      {copied ? '已复制' : '复制'}
      <span aria-live="polite" className="sr-only">
        {liveMessage}
      </span>
    </button>
  );
}
