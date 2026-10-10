import { terminalTheme } from './theme.js';

const CONNECT_TIMEOUT  = 8000;
const RECONNECT_DELAY  = 1500;

export class SimTerminal {
  #term; #fitAddon; #ws = null;
  #connectTimer = null; #reconnectTimer = null;
  #sessionId = null; #closing = false;
  #onStatus;
  #onThemeChange = () => { this.#term.options.theme = terminalTheme(); };

  constructor(el, { onStatus } = {}) {
    this.#onStatus = onStatus ?? (() => {});

    this.#term = new Terminal({
      fontFamily: '"SF Mono", Menlo, Monaco, "Courier New", monospace',
      fontSize: 13,
      lineHeight: 1.4,
      cursorBlink: true,
      scrollback: 5000,
      scrollOnUserInput: false, // don't jump to bottom mid-scroll
      allowProposedApi: true,
      theme: terminalTheme(),
    });

    this.#fitAddon = new FitAddon.FitAddon();
    this.#term.loadAddon(this.#fitAddon);
    this.#term.open(el);
    window.addEventListener('simmer:themechange', this.#onThemeChange);

    this.#term.onData(d => {
      if (this.#ws?.readyState === WebSocket.OPEN) {
        this.#ws.send(new TextEncoder().encode(d));
      }
    });
    this.#term.onResize(({ cols, rows }) => {
      if (this.#ws?.readyState === WebSocket.OPEN) {
        this.#ws.send(JSON.stringify({ type: 'resize', cols, rows }));
      }
    });

    // Focus terminal on click so keyboard input (including Ctrl+C) always reaches it
    el.addEventListener('click', () => this.#term.focus());

    setTimeout(() => { this.#fitAddon.fit(); this.#term.focus(); }, 20);
    this.#connect();
  }

  fit() {
    this.#fitAddon.fit();
  }

  focus() {
    this.#term.focus();
  }

  setFontSize(size) {
    this.#term.options.fontSize = size;
    this.#fitAddon.fit();
  }

  destroy() {
    this.#closing = true;
    window.removeEventListener('simmer:themechange', this.#onThemeChange);
    clearTimeout(this.#connectTimer);
    clearTimeout(this.#reconnectTimer);
    if (this.#ws) { this.#ws.onclose = null; this.#ws.close(); this.#ws = null; }
    this.#term.dispose();
  }

  #connect() {
    clearTimeout(this.#connectTimer);
    clearTimeout(this.#reconnectTimer);
    if (this.#ws) { this.#ws.onclose = null; this.#ws.close(); this.#ws = null; }

    this.#onStatus('connecting');

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws/pty${this.#sessionId ? '?session=' + this.#sessionId : ''}`;
    const ws = new WebSocket(url);
    this.#ws = ws;
    ws.binaryType = 'arraybuffer';

    this.#connectTimer = setTimeout(() => {
      if (ws.readyState === WebSocket.CONNECTING) {
        ws.onclose = null; ws.close(); this.#ws = null;
        this.#reconnectTimer = setTimeout(() => this.#connect(), 1000);
      }
    }, CONNECT_TIMEOUT);

    ws.onopen = () => {
      clearTimeout(this.#connectTimer);
      this.#onStatus('connected');
      this.#fitAddon.fit();
      ws.send(JSON.stringify({ type: 'resize', cols: this.#term.cols, rows: this.#term.rows }));
    };

    ws.onclose = () => {
      clearTimeout(this.#connectTimer);
      if (this.#closing) return;
      if (this.#ws === ws) {
        this.#ws = null;
        this.#onStatus('reconnecting');
        this.#reconnectTimer = setTimeout(() => this.#connect(), RECONNECT_DELAY);
      }
    };

    ws.onerror = () => ws.close();

    ws.onmessage = e => {
      if (e.data instanceof ArrayBuffer) {
        this.#term.write(new Uint8Array(e.data));
      } else {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === 'session') this.#sessionId = msg.id;
        } catch {}
      }
    };
  }
}
