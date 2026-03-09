import { useEffect, useMemo, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { apiRoot } from '@/core/config.js'

function buildWebSocketUrl() {
  const apiUrl = new URL(apiRoot, window.location.origin)
  const scheme = apiUrl.protocol === 'https:' ? 'wss' : 'ws'
  const basePath = apiUrl.pathname.replace(/\/$/, '')
  const wsPath = `${basePath}/platform/webshell/ws`
  return `${scheme}://${apiUrl.host}${wsPath}`
}

export default function PlatformWebShellPage() {
  const terminalRef = useRef(null)
  const xtermRef = useRef(null)
  const wsRef = useRef(null)
  const [status, setStatus] = useState('未连接')
  const wsUrl = useMemo(() => buildWebSocketUrl(), [])

  useEffect(() => {
    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      theme: { background: '#0a0f1f' },
    })
    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(terminalRef.current)
    fitAddon.fit()
    term.writeln('欢迎使用平台 WebShell，连接后可直接管理平台服务器。')
    xtermRef.current = term

    const onResize = () => {
      fitAddon.fit()
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
      }
    }

    const disposable = term.onData((data) => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'input', data }))
      }
    })

    window.addEventListener('resize', onResize)

    return () => {
      disposable.dispose()
      window.removeEventListener('resize', onResize)
      term.dispose()
      if (wsRef.current) wsRef.current.close()
    }
  }, [])

  const connect = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.close()
    }

    const term = xtermRef.current
    if (!term) return

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws
    setStatus('连接中…')
    term.writeln('\r\n[INFO] 正在启动服务器 WebShell...')

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }

    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data)
      if (payload.type === 'connected') {
        setStatus('已连接')
      } else if (payload.type === 'error') {
        setStatus('连接失败')
        term.writeln(`\r\n[ERROR] ${payload.message}`)
      } else if (payload.type === 'data') {
        term.write(payload.data)
      }
    }

    ws.onclose = () => {
      setStatus('已断开')
      term.writeln('\r\n[INFO] WebShell 会话已结束。')
    }
  }

  const disconnect = () => {
    if (wsRef.current) wsRef.current.close()
  }

  return (
    <div className="card card--elevated" style={{ display: 'grid', gap: 16 }}>
      <h2>平台 WebShell</h2>
      <p className="small-muted">仅平台管理员可访问，可直接在平台页面管理当前服务器。</p>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button className="btn" onClick={connect}>连接服务器</button>
        <button className="btn ghost" onClick={disconnect}>断开</button>
        <span className="small-muted">状态：{status}</span>
      </div>

      <div ref={terminalRef} style={{ width: '100%', height: 560, borderRadius: 8, overflow: 'hidden' }} />
    </div>
  )
}
