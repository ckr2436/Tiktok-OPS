import { useEffect, useMemo, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { apiRoot } from '@/core/config.js'

const MAX_INPUT_BUFFERED_AMOUNT = 1024 * 1024
const OUTPUT_FLUSH_INTERVAL_MS = 16
const CTRL_C = '\x03'
const TERMINAL_SHORTCUT_ROWS = [
  [
    { label: 'ESC', data: '\x1b', title: 'Escape' },
    { label: 'TAB', data: '\t', title: 'Tab' },
    { label: '|', data: '|', title: '竖线' },
    { label: '-', data: '-', title: '短横线' },
    { label: '/', data: '/', title: 'Linux 路径分隔符' },
  ],
  [
    { label: 'HOME', data: '\x1b[H', title: '行首' },
    { label: 'END', data: '\x1b[F', title: '行尾' },
    { label: 'PGUP', data: '\x1b[5~', title: '向上翻页' },
    { label: 'PGDN', data: '\x1b[6~', title: '向下翻页' },
    { label: 'INS', data: '\x1b[2~', title: 'Insert' },
    { label: 'DEL', data: '\x1b[3~', title: 'Delete' },
  ],
  [
    { label: '←', data: '\x1b[D', title: '左方向键' },
    { label: '↑', data: '\x1b[A', title: '上方向键' },
    { label: '↓', data: '\x1b[B', title: '下方向键' },
    { label: '→', data: '\x1b[C', title: '右方向键' },
    { label: 'ENTER', data: '\r', title: '回车' },
    { label: '⌫', data: '\x7f', title: '退格' },
  ],
]
const SAFE_COMMAND_TEMPLATE = `pwd
whoami
printf 'UNDER_SCORE_TEST=%s\n' ok`

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

function normalizeCommandInput(data) {
  if (!data) return ''
  return String(data).replace(/\r\n/g, '\n').replace(/\r/g, '\n')
}

function normalizeCommandText(data) {
  const normalized = normalizeCommandInput(data).trimEnd()
  if (!normalized) return ''
  return `${normalized}\n`
}

function wrapAsBashScript(script) {
  const normalized = normalizeCommandInput(script).trimEnd()
  if (!normalized) return ''
  return `bash <<'GMV_WEBSHELL_SCRIPT'\nset -euo pipefail\n${normalized}\nGMV_WEBSHELL_SCRIPT\n`
}

function clampSize(cols, rows) {
  const safeCols = Number.isFinite(Number(cols)) ? Number(cols) : 120
  const safeRows = Number.isFinite(Number(rows)) ? Number(rows) : 30
  return {
    cols: Math.max(20, Math.min(300, safeCols)),
    rows: Math.max(5, Math.min(120, safeRows)),
  }
}

function encodeInputBase64(data) {
  if (!data) return ''
  const bytes = new TextEncoder().encode(String(data))
  let binary = ''
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index])
  }
  return window.btoa(binary)
}

function normalizePastedText(text) {
  if (!text) return ''
  return String(text).replace(/\r\n/g, '\r').replace(/\n/g, '\r')
}

function applyCtrlModifier(data) {
  if (!data || data.length !== 1) return data
  const character = data.toLowerCase()
  if (character >= 'a' && character <= 'z') {
    return String.fromCharCode(character.charCodeAt(0) - 96)
  }
  const ctrlCharacters = {
    '@': '\x00',
    '[': '\x1b',
    '\\': '\x1c',
    ']': '\x1d',
    '^': '\x1e',
    '_': '\x1f',
    '?': '\x7f',
  }
  return ctrlCharacters[data] ?? data
}

function applyTerminalModifiers(data, ctrl, alt) {
  if (!data || (!ctrl && !alt)) return data
  const modifierCode = ctrl && alt ? 7 : ctrl ? 5 : 3
  const cursorKey = data.match(/^\x1b\[([A-DHF])$/)
  if (cursorKey) return `\x1b[1;${modifierCode}${cursorKey[1]}`
  const tildeKey = data.match(/^\x1b\[(\d+)~$/)
  if (tildeKey) return `\x1b[${tildeKey[1]};${modifierCode}~`

  const ctrlData = ctrl ? applyCtrlModifier(data) : data
  return alt ? `\x1b${ctrlData}` : ctrlData
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
  const ctrlActiveRef = useRef(false)
  const altActiveRef = useRef(false)
  const [status, setStatus] = useState('未连接')
  const [commandText, setCommandText] = useState('')
  const [ctrlActive, setCtrlActive] = useState(false)
  const [altActive, setAltActive] = useState(false)
  const wsUrlCandidates = useMemo(() => buildWebSocketUrlCandidates(), [])

  const focusTerminal = () => {
    const node = terminalRef.current
    const term = xtermRef.current
    if (node && typeof node.focus === 'function') {
      try {
        node.focus({ preventScroll: true })
      } catch {
        node.focus()
      }
    }
    if (term) {
      try {
        term.focus()
      } catch {
        // ignored
      }
    }
  }

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
    outputFlushTimerRef.current = window.setTimeout(() => flushOutput(false), OUTPUT_FLUSH_INTERVAL_MS)
  }

  const sendJson = (payload) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    ws.send(JSON.stringify(payload))
    return true
  }

  const sendRawInput = (data) => {
    if (!data) return false
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setStatus('未连接')
      const term = xtermRef.current
      if (term) term.writeln('\r\n[WARN] WebShell 未连接，无法发送命令。')
      return false
    }
    if (ws.bufferedAmount > MAX_INPUT_BUFFERED_AMOUNT) {
      setStatus('网络拥塞')
      return false
    }
    const dataB64 = encodeInputBase64(data)
    if (!dataB64) return false
    return sendJson({ type: 'input_b64', data_b64: dataB64 })
  }

  const setModifier = (modifier, active) => {
    if (modifier === 'ctrl') {
      ctrlActiveRef.current = active
      setCtrlActive(active)
    } else {
      altActiveRef.current = active
      setAltActive(active)
    }
  }

  const sendModifiedInput = (data) => {
    if (!data) return false
    const ctrl = ctrlActiveRef.current
    const alt = altActiveRef.current
    const modifiedData = applyTerminalModifiers(data, ctrl, alt)
    const sent = sendRawInput(modifiedData)
    if (sent) {
      if (ctrl) setModifier('ctrl', false)
      if (alt) setModifier('alt', false)
    }
    return sent
  }

  const sendShortcut = (data) => {
    sendModifiedInput(data)
    window.setTimeout(focusTerminal, 0)
  }

  const sendResize = () => {
    const term = xtermRef.current
    if (!term) return
    if (fitAddonRef.current) {
      try {
        fitAddonRef.current.fit()
      } catch {
        // xterm can throw while the container is hidden.
      }
    }
    const size = clampSize(term.cols, term.rows)
    sendJson({ type: 'resize', ...size })
  }

  const scheduleResize = () => {
    if (resizeTimerRef.current) window.clearTimeout(resizeTimerRef.current)
    resizeTimerRef.current = window.setTimeout(sendResize, 120)
  }

  const handleTerminalPaste = (event) => {
    const text = event.clipboardData?.getData('text') || ''
    const data = normalizePastedText(text)
    if (!data) return
    event.preventDefault()
    event.stopPropagation()
    sendRawInput(data)
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
    // xterm writes mobile/IME input through its hidden textarea. Listening to
    // onData is required because soft keyboards often emit no usable keydown.
    const inputDisposable = term.onData((data) => sendModifiedInput(data))
    const helperTextarea = terminalRef.current?.querySelector('.xterm-helper-textarea')
    if (helperTextarea) {
      helperTextarea.setAttribute('inputmode', 'text')
      helperTextarea.setAttribute('enterkeyhint', 'enter')
      helperTextarea.setAttribute('autocapitalize', 'off')
      helperTextarea.setAttribute('autocomplete', 'off')
      helperTextarea.setAttribute('autocorrect', 'off')
      helperTextarea.setAttribute('spellcheck', 'false')
    }
    fitAddon.fit()
    term.writeln('欢迎使用平台 WebShell，连接后可直接管理平台服务器。')
    term.writeln('提示：输入已经改为 base64 字节传输，用于避免下划线和控制字符被改写。')
    term.writeln('提示：进入 vim/nano/top 后，请点击黑色终端区域，确保光标焦点在终端内。')
    xtermRef.current = term
    fitAddonRef.current = fitAddon
    const onResize = () => scheduleResize()
    const resizeDisposable = typeof term.onResize === 'function' ? term.onResize(() => scheduleResize()) : null
    window.addEventListener('resize', onResize)
    return () => {
      manuallyClosedRef.current = true
      inputDisposable.dispose()
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
        ws.send(JSON.stringify({ type: 'init', shell: '/bin/bash -li', ...size }))
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
          window.setTimeout(focusTerminal, 0)
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
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) sendJson({ type: 'close' })
    if (wsRef.current) wsRef.current.close()
    connectedRef.current = false
    setStatus('已断开')
  }

  const interrupt = () => {
    sendRawInput(CTRL_C)
    window.setTimeout(focusTerminal, 0)
  }

  const toggleModifier = (modifier) => {
    const active = modifier === 'ctrl' ? ctrlActiveRef.current : altActiveRef.current
    setModifier(modifier, !active)
    window.setTimeout(focusTerminal, 0)
  }

  const runCommand = () => {
    const payload = normalizeCommandText(commandText)
    if (sendRawInput(payload)) {
      setCommandText('')
      window.setTimeout(focusTerminal, 0)
    }
  }

  const runAsScript = () => {
    const payload = wrapAsBashScript(commandText)
    if (sendRawInput(payload)) {
      setCommandText('')
      window.setTimeout(focusTerminal, 0)
    }
  }

  return (
    <div className="card card--elevated" style={{ display: 'grid', gap: 16 }}>
      <h2>平台 WebShell</h2>
      <p className="small-muted">仅平台管理员可访问，可直接在平台页面管理当前服务器。</p>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="btn" onClick={connect}>连接服务器</button>
        <button className="btn ghost" onClick={disconnect}>断开</button>
        <button className="btn ghost" onClick={interrupt}>发送 Ctrl+C</button>
        <span className="small-muted">状态：{status}</span>
      </div>
      <div
        ref={terminalRef}
        tabIndex={0}
        role="application"
        aria-label="WebShell terminal"
        onMouseDown={() => window.setTimeout(focusTerminal, 0)}
        onTouchStart={() => window.setTimeout(focusTerminal, 0)}
        onClick={() => window.setTimeout(focusTerminal, 0)}
        onPasteCapture={handleTerminalPaste}
        style={{ width: '100%', height: 520, borderRadius: 8, overflow: 'hidden', outline: 'none', touchAction: 'manipulation' }}
      />
      <div style={{ display: 'grid', gap: 6, minWidth: 0 }}>
        <div className="small-muted">快捷键（CTRL / ALT 点亮后作用于下一次按键）</div>
        <div role="toolbar" aria-label="WebShell 快捷键" style={{ display: 'grid', gap: 6, padding: '2px 2px 4px' }}>
          {TERMINAL_SHORTCUT_ROWS.map((shortcuts, rowIndex) => (
            <div
              key={shortcuts.map((shortcut) => shortcut.label).join('-')}
              role="group"
              aria-label={`快捷键第 ${rowIndex + 1} 行`}
              style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}
            >
              {rowIndex === 0 && (
                <>
                  <button
                    type="button"
                    className={ctrlActive ? 'btn' : 'btn ghost'}
                    aria-pressed={ctrlActive}
                    title="点亮后与下一次按键组合"
                    onClick={() => toggleModifier('ctrl')}
                    style={{ flex: '0 0 auto', minWidth: 58 }}
                  >
                    CTRL
                  </button>
                  <button
                    type="button"
                    className={altActive ? 'btn' : 'btn ghost'}
                    aria-pressed={altActive}
                    title="点亮后与下一次按键组合"
                    onClick={() => toggleModifier('alt')}
                    style={{ flex: '0 0 auto', minWidth: 52 }}
                  >
                    ALT
                  </button>
                </>
              )}
              {shortcuts.map((shortcut) => (
                <button
                  type="button"
                  className="btn ghost"
                  key={shortcut.label}
                  title={shortcut.title}
                  aria-label={shortcut.title}
                  onClick={() => sendShortcut(shortcut.data)}
                  style={{ flex: '0 0 auto', minWidth: shortcut.label.length > 3 ? 58 : 42 }}
                >
                  {shortcut.label}
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>
      <div style={{ display: 'grid', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
          <strong>安全命令输入</strong>
          <span className="small-muted">命令会以 base64 字节传输，适合下划线、~、Ctrl+C 后恢复、多行脚本。</span>
        </div>
        <textarea
          value={commandText}
          onChange={(event) => setCommandText(event.target.value)}
          placeholder={'例如：printf \'UNDER_SCORE_TEST=%s\\n\' ok'}
          spellCheck={false}
          style={{
            width: '100%',
            minHeight: 120,
            boxSizing: 'border-box',
            resize: 'vertical',
            borderRadius: 8,
            border: '1px solid var(--border-color, #d9d9d9)',
            padding: 12,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
            fontSize: 13,
          }}
        />
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn" onClick={runCommand}>发送命令</button>
          <button className="btn ghost" onClick={runAsScript}>作为 bash 脚本执行</button>
          <button className="btn ghost" onClick={() => setCommandText(SAFE_COMMAND_TEMPLATE)}>填入测试模板</button>
          <button className="btn ghost" onClick={() => setCommandText('')}>清空输入框</button>
        </div>
      </div>
    </div>
  )
}
