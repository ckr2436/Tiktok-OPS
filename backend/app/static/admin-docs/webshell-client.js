/* eslint-disable no-console */
/**
 * GMV Ops WebShell browser client.
 *
 * Production goals:
 * - no local echo: only backend PTY output is written into xterm;
 * - normalize Enter from CR/CRLF to LF before sending, preventing ^M;
 * - send explicit init/resize/close messages expected by router_webshell.py;
 * - debounce resize and buffer output writes to avoid broken escape sequences;
 * - respond to application-level ping frames so the backend can keep idle
 *   WebSocket/PTY sessions healthy behind reverse proxies.
 *
 * Usage:
 *   const term = new Terminal({ cursorBlink: true, convertEol: false });
 *   const fitAddon = new FitAddon.FitAddon();
 *   term.loadAddon(fitAddon);
 *   term.open(document.getElementById('terminal'));
 *   const shell = new window.GMVWebShellClient({ terminal: term, fitAddon });
 *   shell.connect();
 */
(function attachGMVWebShell(global) {
  'use strict';

  const DEFAULT_PATH = '/api/v1/platform/webshell/ws';
  const MAX_INPUT_BUFFERED_AMOUNT = 1024 * 1024;
  const OUTPUT_FLUSH_INTERVAL_MS = 16;

  function buildDefaultUrl(path) {
    const protocol = global.location && global.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = global.location ? global.location.host : '';
    return `${protocol}//${host}${path || DEFAULT_PATH}`;
  }

  function normalizeInput(data) {
    if (!data) return '';
    return String(data).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  }

  function safeJsonParse(raw) {
    try {
      return JSON.parse(raw);
    } catch (_error) {
      return null;
    }
  }

  function clampDimension(value, min, max, fallback) {
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(min, Math.min(max, parsed));
  }

  function terminalSize(terminal) {
    return {
      cols: clampDimension(terminal && terminal.cols, 20, 300, 120),
      rows: clampDimension(terminal && terminal.rows, 5, 120, 30),
    };
  }

  class GMVWebShellClient {
    constructor(options) {
      const opts = options || {};
      if (!opts.terminal) {
        throw new Error('GMVWebShellClient requires an xterm Terminal instance.');
      }

      this.terminal = opts.terminal;
      this.fitAddon = opts.fitAddon || null;
      this.url = opts.url || buildDefaultUrl(opts.path);
      this.shell = opts.shell || undefined;
      this.onStatus = typeof opts.onStatus === 'function' ? opts.onStatus : function noop() {};
      this.onMessage = typeof opts.onMessage === 'function' ? opts.onMessage : function noop() {};
      this.reconnect = Boolean(opts.reconnect);
      this.reconnectDelayMs = opts.reconnectDelayMs || 1500;

      this.ws = null;
      this.connected = false;
      this.disposed = false;
      this.sessionId = null;
      this.outputBuffer = '';
      this.flushTimer = null;
      this.resizeTimer = null;
      this.inputDisposable = null;
      this.resizeDisposable = null;
    }

    connect() {
      if (this.disposed) return;
      if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
        return;
      }

      this.onStatus('connecting');
      const ws = new WebSocket(this.url);
      this.ws = ws;

      ws.onopen = () => {
        const size = terminalSize(this.terminal);
        this._send({
          type: 'init',
          shell: this.shell,
          cols: size.cols,
          rows: size.rows,
        });
        this._bindTerminal();
      };

      ws.onmessage = (event) => {
        this._handleMessage(event.data);
      };

      ws.onerror = () => {
        this.onStatus('error');
      };

      ws.onclose = () => {
        const wasConnected = this.connected;
        this.connected = false;
        this.sessionId = null;
        this._unbindTerminal();
        this._flushOutput(true);
        this.onStatus('closed');

        if (!this.disposed && this.reconnect && wasConnected) {
          global.setTimeout(() => this.connect(), this.reconnectDelayMs);
        }
      };
    }

    disconnect() {
      this.disposed = true;
      this._send({ type: 'close' });
      this._unbindTerminal();
      if (this.ws) {
        try {
          this.ws.close();
        } catch (_error) {
          // ignored
        }
      }
      this.ws = null;
      this._flushOutput(true);
    }

    fitAndResize() {
      if (this.fitAddon && typeof this.fitAddon.fit === 'function') {
        try {
          this.fitAddon.fit();
        } catch (_error) {
          // xterm can throw while hidden; still send the last known size.
        }
      }
      this._debouncedResize();
    }

    _bindTerminal() {
      if (this.inputDisposable) return;

      this.inputDisposable = this.terminal.onData((rawData) => {
        const data = normalizeInput(rawData);
        if (!data) return;
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        if (this.ws.bufferedAmount > MAX_INPUT_BUFFERED_AMOUNT) {
          this.onStatus('backpressure');
          return;
        }
        this._send({ type: 'input', data });
      });

      if (typeof this.terminal.onResize === 'function') {
        this.resizeDisposable = this.terminal.onResize(() => this._debouncedResize());
      }

      if (global.addEventListener) {
        global.addEventListener('resize', this._windowResizeHandler || (this._windowResizeHandler = () => this.fitAndResize()));
      }
    }

    _unbindTerminal() {
      if (this.inputDisposable && typeof this.inputDisposable.dispose === 'function') {
        this.inputDisposable.dispose();
      }
      if (this.resizeDisposable && typeof this.resizeDisposable.dispose === 'function') {
        this.resizeDisposable.dispose();
      }
      if (global.removeEventListener && this._windowResizeHandler) {
        global.removeEventListener('resize', this._windowResizeHandler);
      }
      this.inputDisposable = null;
      this.resizeDisposable = null;
    }

    _debouncedResize() {
      if (this.resizeTimer) {
        global.clearTimeout(this.resizeTimer);
      }
      this.resizeTimer = global.setTimeout(() => {
        const size = terminalSize(this.terminal);
        this._send({ type: 'resize', cols: size.cols, rows: size.rows });
      }, 120);
    }

    _handleMessage(raw) {
      const payload = typeof raw === 'string' ? safeJsonParse(raw) : null;
      if (!payload || typeof payload !== 'object') {
        return;
      }

      this.onMessage(payload);

      if (payload.type === 'connected') {
        this.connected = true;
        this.sessionId = payload.session_id || null;
        this.onStatus('connected', payload);
        this.fitAndResize();
        return;
      }

      if (payload.type === 'data') {
        this._queueOutput(payload.data || '');
        return;
      }

      if (payload.type === 'ping') {
        this._send({ type: 'pong', session_id: payload.session_id || this.sessionId || undefined });
        return;
      }

      if (payload.type === 'error') {
        this._queueOutput(`\r\n\x1b[31m${payload.message || 'WebShell error'}\x1b[0m\r\n`);
        this.onStatus('error', payload);
        return;
      }

      if (payload.type === 'exit') {
        const reason = payload.reason ? ` (${payload.reason})` : '';
        this._queueOutput(`\r\n\x1b[33m[WebShell closed${reason}]\x1b[0m\r\n`);
        this.onStatus('exit', payload);
      }
    }

    _queueOutput(data) {
      if (!data) return;
      this.outputBuffer += String(data);
      if (this.flushTimer) return;
      this.flushTimer = global.setTimeout(() => this._flushOutput(false), OUTPUT_FLUSH_INTERVAL_MS);
    }

    _flushOutput(force) {
      if (this.flushTimer) {
        global.clearTimeout(this.flushTimer);
        this.flushTimer = null;
      }
      if (!this.outputBuffer && !force) return;
      const chunk = this.outputBuffer;
      this.outputBuffer = '';
      if (chunk) {
        this.terminal.write(chunk);
      }
    }

    _send(payload) {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
      this.ws.send(JSON.stringify(payload));
      return true;
    }
  }

  GMVWebShellClient.normalizeInput = normalizeInput;
  GMVWebShellClient.buildDefaultUrl = buildDefaultUrl;

  global.GMVWebShellClient = GMVWebShellClient;
})(typeof window !== 'undefined' ? window : globalThis);
