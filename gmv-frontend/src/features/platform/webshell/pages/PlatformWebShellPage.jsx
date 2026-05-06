import { useEffect, useMemo, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { apiRoot } from '@/core/config.js'

const MAX_INPUT_BUFFERED_AMOUNT = 1024 * 1024
const OUTPUT_FLUSH_INTERVAL_MS = 16

function buildWebSocketUrlCandidates() {
  const apiUrl = new URL(apiRoot, window.location.origin)
  const scheme = apiUrl.protocol === 'https:' ? 'wss' : 'ws'
  const basePath = apiUrl.pathname.replace(/\/$/, '')
  const paths = [
    `${basePath}/platform/webshell/ws`,
    '/api/v1/platform/webshell/ws',
    '/api/platform/webshell/ws',
  ]

  return [...new Set(paths)].map((path) => `${scheme}://${apiUrl.host}${path}`)
}

function normalizeTerminalInput(data) {
  if (!data) return ''
  // xterm/browser Enter is usually CR. Normalize it before sending to the PTY
  // so production shells do not render Enter as ^M.
  return String(data).replace(/\r\n/g, '\n').replace(/\r/g, '\n')
}

function clampSize(cols, rows) {
  const safeCols = Number.isFinite(Number(cols)) ? Number(cols) : 120
  const safeRows = Number.isFinite(Number(rows)) ? Number(rows) : 30
  return {
    cols: Math.max(20, Math.min(300, safeCols)),
    rows: Math.max(5, Math.min(120, safeRows)),
  }
}

export default function PlatformWebShellPage() {
  const terminalRef = useRef(null)
  const xtermRef = useRef(null)
  const fitAddonRef = useRef(null)
  const wsRef = useRef(null)
  const outputBufferRef = useRef('')
  const outputFlushTimerRef = useRef(null)
  const resizeTimerRef = useRef(null)
  const connectedRef = useRef(false)
  const manuallyClosedRef = useRef(false)
  const [status, setStatus] = useState('未连接')
  const wsUrlCandidates = useMemo(() => buildWebSocketUrlCandidates(), [])

  const flushOutput = (force = false) => {
    if (outputFlushTimerRef.current) {
      window.clearTimeout(outputFlushTimerRef.current)
      outputFlushTimerRef.current = null
    }

    const term = xtermRef.current
    const chunk = outputBufferRef.current
    if (!term || (!chunk && !force)) return

    outputBufferRef.current = ''
    if (chunk) term.write(chunk)
  }

  const queueOutput = (data) => {
    if (!data) return
    outputBufferRef.current += String(data)
    if (outputFlushTimerRef.current) return

    outputFlushTimerRef.current = window.setTimeout(() => {
      flushOutput(false)
    }, OUTPUT_FLUSH_INTERVAL_MS)
  }

  const sendJson = (payload) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    ws.send(JSON.stringify(payload))
    return true
  }

  const sendResize = () => {
    const term = xtermRef.current
    if (!term) return

    if (fitAddonRef.current) {
      try {
        fitAddonRef.current.fit()
      } catch {
        // xterm can throw while the container is hidden; still send last size.
      }
    }

    const size = clampSize(term.cols, term.rows)
    sendJson({ type: 'resize', ...size })
  }

  const scheduleResize = () => {
    if (resizeTimerRef.current) window.clearTimeout(resizeTimerRef.current)
    resizeTimerRef.current = window.setTimeout(sendResize, 120)
  }

  useEffect(() => {
    const term = new Terminal({
      cursorBlink: true,
      convertEol: false,
      fontSize: 13,
      scrollback: 5000,
      theme: { background: '#0a0f1f' },
    })
    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(terminalRef.current)
    fitAddon.fit()
    term.writeln('欢迎使用平台 WebShell，连接后可直接管理平台服务器。')
    term.writeln('提示：生产环境会记录 WebShell 会话审计，请谨慎操作。')
    xtermRef.current = term
    fitAddonRef.current = fitAddon

    const onResize = () => scheduleResize()

    const dataDisposable = term.onData((rawData) => {
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      if (ws.bufferedAmount > MAX_INPUT_BUFFERED_AMOUNT) {
        setStatus('网络拥塞')
        return
      }

      const data = normalizeTerminalInput(rawData)
      if (data) sendJson({ type: 'input', data })
    })

    const resizeDisposable = typeof term.onResize === 'function'
      ? term.onResize(() => scheduleResize())
      : null

    window.addEventListener('resize', onResize)

    return () => {
      manuallyClosedRef.current = true
      dataDisposable.dispose()
      if (resizeDisposable) resizeDisposable.dispose()
      window.removeEventListener('resize', onResize)
      if (resizeTimerRef.current) window.clearTimeout(resizeTimerRef.current)
      if (outputFlushTimerRef.current) window.clearTimeout(outputFlushTimerRef.current)
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        sendJson({ type: 'close' })
        wsRef.current.close()
      }
      wsRef.current = null
      term.dispose()
      xtermRef.current = null
      fitAddonRef.current = null
    }
  }, [])

  const connect = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      sendJson({ type: 'close' })
      wsRef.current.close()
    }

    const term = xtermRef.current
    if (!term) return

    manuallyClosedRef.current = false
    connectedRef.current = false
    setStatus('连接中…')
    term.writeln('\r\n[INFO] 正在启动服务器 WebShell...')

    let attemptIndex = 0

    const connectWithCandidate = () => {
      if (manuallyClosedRef.current) return

      if (attemptIndex >= wsUrlCandidates.length) {
        setStatus('连接失败')
        term.writeln('\r\n[ERROR] 所有 WebSocket 地址均连接失败，请检查反向代理 Upgrade 与路径重写。')
        return
      }

      const wsUrl = wsUrlCandidates[attemptIndex]
      attemptIndex += 1
      term.writeln(`\r\n[INFO] 正在尝试连接：${wsUrl}`)

      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        const size = clampSize(term.cols, term.rows)
        ws.send(JSON.stringify({
          type: 'init',
          shell: '/bin/bash -li',
          ...size,
        }))
      }

      ws.onmessage = (event) => {
        let payload
        try {
          payload = JSON.parse(event.data)
        } catch {
          term.writeln('\r\n[ERROR] 收到无法解析的服务端消息。')
          return
        }

        if (payload.type === 'connected') {
          connectedRef.current = true
          setStatus('已连接')
          scheduleResize()
        } else if (payload.type === 'error') {
          setStatus('连接失败')
          queueOutput(`\r\n\x1b[31m[ERROR] ${payload.message || 'WebShell error'}\x1b[0m\r\n`)
        } else if (payload.type === 'data') {
          queueOutput(payload.data)
        } else if (payload.type === 'ping') {
          ws.send(JSON.stringify({ type: 'pong', session_id: payload.session_id }))
        } else if (payload.type === 'exit') {
          const reason = payload.reason ? `，原因：${payload.reason}` : ''
          queueOutput(`\r\n\x1b[33m[INFO] WebShell 会话已结束${reason}。\x1b[0m\r\n`)
          setStatus('已断开')
        }
      }

      ws.onerror = () => {
        if (!connectedRef.current) {
          term.writeln('\r\n[WARN] WebSocket 握手或网络异常，准备尝试下一个候选地址。')
        } else {
          setStatus('连接错误')
          term.writeln('\r\n[ERROR] WebSocket 网络异常。')
        }
      }

      ws.onclose = (event) => {
        flushOutput(true)
        const reason = event.reason ? `，原因：${event.reason}` : ''
        term.writeln(`\r\n[INFO] WebShell 会话已结束（code=${event.code}${reason}）。`)

        if (!manuallyClosedRef.current && !connectedRef.current && (event.code === 1006 || event.code === 1002 || event.code === 1005)) {
          connectWithCandidate()
          return
        }

        if (event.code === 1008) {
          term.writeln('\r\n[ERROR] 权限不足或安全策略拒绝：请确认已登录平台管理员账号，并检查 WEBSHELL_ENABLED / Origin / 会话上限。')
        } else if (event.code === 1006) {
          term.writeln('\r\n[ERROR] 连接异常中断：可能是路径不存在、代理未转发 Upgrade 或网络中断。')
        }

        connectedRef.current = false
        setStatus('已断开')
      }
    }

    connectWithCandidate()
  }

  const disconnect = () => {
    manuallyClosedRef.current = true
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      sendJson({ type: 'close' })
    }
    if (wsRef.current) wsRef.current.close()
    connectedRef.current = false
    setStatus('已断开')
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
