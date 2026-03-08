import { useEffect, useMemo, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { apiRoot } from '@/core/config.js'

function buildWebSocketUrl() {
  const apiUrl = new URL(apiRoot, window.location.origin)
  const scheme = apiUrl.protocol === 'https:' ? 'wss' : 'ws'
  const basePath = apiUrl.pathname.replace(/\/$/, '')
  const wsPath = `${basePath}/platform/webssh/ws`
  return `${scheme}://${apiUrl.host}${wsPath}`
}

export default function PlatformWebSshPage() {
  const terminalRef = useRef(null)
  const xtermRef = useRef(null)
  const wsRef = useRef(null)
  const [status, setStatus] = useState('未连接')
  const [form, setForm] = useState({
    host: '',
    port: 22,
    username: '',
    password: '',
    privateKey: '',
    passphrase: '',
    authMethod: 'password',
  })

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
    term.writeln('欢迎使用平台 WebSSH，连接后即可开始操作。')
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

  const setField = (key, value) => setForm((prev) => ({ ...prev, [key]: value }))

  const connect = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.close()
    }

    const term = xtermRef.current
    if (!term) return

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws
    setStatus('连接中…')
    term.writeln('\r\n[INFO] 正在建立 SSH 连接...')

    ws.onopen = () => {
      ws.send(JSON.stringify(form))
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
      term.writeln('\r\n[INFO] SSH 会话已结束。')
    }
  }

  const disconnect = () => {
    if (wsRef.current) wsRef.current.close()
  }

  return (
    <div className="card card--elevated" style={{ display: 'grid', gap: 16 }}>
      <h2>平台 WebSSH</h2>
      <p className="small-muted">仅平台管理员可访问，租户账号无权限。</p>

      <div style={{ display: 'grid', gap: 8, gridTemplateColumns: 'repeat(3, minmax(0,1fr))' }}>
        <input className="input" placeholder="SSH Host" value={form.host} onChange={(e) => setField('host', e.target.value)} />
        <input className="input" placeholder="Port" type="number" value={form.port} onChange={(e) => setField('port', Number(e.target.value || 22))} />
        <input className="input" placeholder="Username" value={form.username} onChange={(e) => setField('username', e.target.value)} />
        <select className="input" value={form.authMethod} onChange={(e) => setField('authMethod', e.target.value)}>
          <option value="password">密码认证</option>
          <option value="privateKey">私钥认证</option>
        </select>
        <input
          className="input"
          placeholder={form.authMethod === 'password' ? 'Password（必填）' : 'Password（未使用）'}
          type="password"
          disabled={form.authMethod !== 'password'}
          value={form.password}
          onChange={(e) => setField('password', e.target.value)}
        />
        <input
          className="input"
          placeholder={form.authMethod === 'privateKey' ? '私钥 Passphrase（可选）' : '私钥 Passphrase（未使用）'}
          type="password"
          disabled={form.authMethod !== 'privateKey'}
          value={form.passphrase}
          onChange={(e) => setField('passphrase', e.target.value)}
        />
      </div>

      <textarea
        className="input"
        rows={6}
        placeholder={form.authMethod === 'privateKey' ? '粘贴 SSH 私钥（必填）' : '粘贴 SSH 私钥（未使用）'}
        disabled={form.authMethod !== 'privateKey'}
        value={form.privateKey}
        onChange={(e) => setField('privateKey', e.target.value)}
      />

      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button className="btn" onClick={connect}>连接</button>
        <button className="btn ghost" onClick={disconnect}>断开</button>
        <span className="small-muted">状态：{status}</span>
      </div>

      <div ref={terminalRef} style={{ width: '100%', height: 520, borderRadius: 8, overflow: 'hidden' }} />
    </div>
  )
}
