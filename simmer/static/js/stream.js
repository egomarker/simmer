const CONNECT_TIMEOUT = 5000;
const RECONNECT_DELAY = 2000;
const NO_FRAME_WARN   = 4000;
const WATCHDOG_POLL   = 3000;
const WATCHDOG_STALE  = 8000;
const DRAG_THRESHOLD  = 0.015;

export class SimStream {
  #liveTouch = false;
  #udid; #canvas; #ctx;
  #ws = null; #connectTimer = null; #reconnectTimer = null; #watchdog = null;
  #lastFrameAt = 0; #firstFrame = false; #frameInFlight = false;
  #portraitW; #portraitH; #isLandscape = false;
  #onStatus; #onFirstFrame; #onOrientationChange; #onRotateStart; #onRotateEnd;
  #onStats; #statsTimer = null;
  #stats = { frames: 0, bytes: 0, dropped: 0, lastFrames: 0, lastBytes: 0, fps: 0, bps: 0, avgFrame: 0 };
  #serverStats = { fps: null, quality: null };

  constructor(
    udid,
    canvas,
    {
      fps = 15,
      quality = 70,
      onStatus,
      onFirstFrame,
      onOrientationChange,
      onRotateStart,
      onRotateEnd,
      onStats,
    } = {}
  ) {
    this.#udid = udid;
    this.#canvas = canvas;
    this.#ctx = canvas.getContext('2d');
    this.#portraitW = canvas.width;
    this.#portraitH = canvas.height;
    this.#onStatus = onStatus ?? (() => {});
    this.#onFirstFrame = onFirstFrame ?? (() => {});
    this.#onOrientationChange = onOrientationChange ?? (() => {});
    this.#onRotateStart = onRotateStart ?? (() => {});
    this.#onRotateEnd = onRotateEnd ?? (() => {});
    this.#onStats = onStats ?? (() => {});

    this.settings = { fps, quality, data_saver: false, dev_w: canvas.width, dev_h: canvas.height };

    this.#setupPointer();
    this.#connect();
  }

  send(msg) {
    if (msg?.type === 'rotate') this.#onRotateStart();
    if (this.#ws?.readyState === WebSocket.OPEN) {
      this.#ws.send(JSON.stringify(msg));
    }
  }

  updateSettings(patch) {
    Object.assign(this.settings, patch);
    this.send({ type: 'settings', ...patch });
  }

  destroy() {
    clearTimeout(this.#connectTimer);
    clearTimeout(this.#reconnectTimer);
    clearInterval(this.#watchdog);
    clearInterval(this.#statsTimer);
    if (this.#ws) { this.#ws.onclose = null; this.#ws.close(); this.#ws = null; }
  }

  #setOrientation(landscape) {
    if (landscape === this.#isLandscape) return;
    this.#isLandscape = landscape;
    this.#canvas.width  = landscape ? this.#portraitH : this.#portraitW;
    this.#canvas.height = landscape ? this.#portraitW : this.#portraitH;
    this.send({ type: 'settings', dev_w: this.#canvas.width, dev_h: this.#canvas.height });
    this.#onOrientationChange(this.#canvas.width, this.#canvas.height);
  }

  #connect() {
    clearTimeout(this.#connectTimer);
    clearTimeout(this.#reconnectTimer);
    if (this.#ws) { this.#ws.onclose = null; this.#ws.close(); }

    this.#firstFrame = false;
    this.#liveTouch = false;
    this.#onStatus('connecting');

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(
      `${proto}//${location.host}/ws/${this.#udid}?fps=${this.settings.fps}&quality=${this.settings.quality}&w=${this.#portraitW}&h=${this.#portraitH}`
    );
    this.#ws = ws;
    ws.binaryType = 'blob';

    this.#connectTimer = setTimeout(() => {
      if (ws.readyState === WebSocket.CONNECTING) {
        ws.onclose = null; ws.close();
        this.#reconnectTimer = setTimeout(() => this.#connect(), 1000);
      }
    }, CONNECT_TIMEOUT);

    let noFrameTimer = null;

    ws.onopen = () => {
      clearTimeout(this.#connectTimer);
      this.#onStatus('connected');
      noFrameTimer = setTimeout(() => {
        if (!this.#firstFrame) this.#onStatus('no-frames');
      }, NO_FRAME_WARN);
    };

    ws.onclose = () => {
      clearTimeout(this.#connectTimer);
      clearTimeout(noFrameTimer);
      if (this.#ws === ws) {
        this.#onStatus('reconnecting');
        this.#reconnectTimer = setTimeout(() => this.#connect(), RECONNECT_DELAY);
      }
    };

    ws.onerror = () => ws.close();

    ws.onmessage = e => {
      if (typeof e.data === 'string') {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === 'input_capabilities') {
            this.#liveTouch = msg.live_touch === true;
          } else if (msg.type === 'rotated') {
            this.#setOrientation(!this.#isLandscape);
            this.#onRotateEnd(true);
          } else if (msg.type === 'rotate_failed') {
            this.#onRotateEnd(false);
          } else if (msg.type === 'server_stats') {
            this.#serverStats = { fps: msg.fps ?? null, quality: msg.quality ?? null };
          }
        } catch {}
        return;
      }
      if (!(e.data instanceof Blob)) return;

      // Acknowledge receipt immediately, independently of rendering.
      this.send({ type: 'frame_ack' });

      this.#lastFrameAt = Date.now();
      this.#stats.bytes += e.data.size || 0;
      if (!this.#firstFrame) {
        this.#firstFrame = true;
        clearTimeout(noFrameTimer);
        this.#onStatus('streaming');
        this.#onFirstFrame();
      }
      if (this.#frameInFlight) {
        this.#stats.dropped += 1;
        //this.send({ type: 'frame_ack', dropped: true });
        return;
      }
      this.#frameInFlight = true;

    const started = performance.now();

    createImageBitmap(e.data)
      .then(bitmap => {
        const decodeMs = performance.now() - started;
        const drawStart = performance.now();

        try {
          this.#setOrientation(bitmap.width > bitmap.height);
          this.#ctx.drawImage(
            bitmap, 0, 0,
            this.#canvas.width,
            this.#canvas.height
          );
          this.#stats.frames += 1;

          if (this.#stats.frames % 30 === 0) {
            console.log(
              `Decode: ${decodeMs.toFixed(1)} ms, ` +
              `Draw: ${(performance.now() - drawStart).toFixed(1)} ms`
            );
          }

          //this.send({ type: 'frame_ack' });
        } catch (err) {
          console.error('Frame drawing failed:', err);
          //this.send({ type: 'frame_ack', decode_error: true });
        } finally {
          bitmap.close();
          this.#frameInFlight = false;
        }
      })
      .catch(err => {
        console.error('Frame decoding failed:', err);
        this.#frameInFlight = false;
        //this.send({ type: 'frame_ack', decode_error: true });
      });

    };

    clearInterval(this.#watchdog);
    this.#watchdog = setInterval(() => {
      if (this.#ws?.readyState === WebSocket.OPEN &&
          this.#lastFrameAt > 0 &&
          Date.now() - this.#lastFrameAt > WATCHDOG_STALE) {
        this.#connect();
      }
    }, WATCHDOG_POLL);

    clearInterval(this.#statsTimer);
    this.#statsTimer = setInterval(() => {
      const frames = this.#stats.frames - this.#stats.lastFrames;
      const bytes = this.#stats.bytes - this.#stats.lastBytes;
      this.#stats.lastFrames = this.#stats.frames;
      this.#stats.lastBytes = this.#stats.bytes;
      this.#stats.fps = frames;
      this.#stats.bps = bytes;
      this.#stats.avgFrame = frames ? bytes / frames : this.#stats.avgFrame;
      this.#onStats({
        fps: this.#stats.fps,
        bps: this.#stats.bps,
        avgFrame: this.#stats.avgFrame,
        dropped: this.#stats.dropped,
        serverFps: this.#serverStats.fps,
        serverQuality: this.#serverStats.quality,
      });
    }, 1000);
  }

  #setupPointer() {
    const canvas = this.#canvas;
    canvas.style.touchAction = 'none';

    let activePointer = null;
    let origin = null;
    let live = false;
    let dragging = false;
    let pending = null;
    let raf = 0;
    let lastSentAt = 0;
    let lastSent = null;

    const norm = e => {
      const r = canvas.getBoundingClientRect();
      return {
        x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
        y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)),
      };
    };

    const sendMove = p => {
      this.send({ type: 'touch_move', x: p.x, y: p.y });
      lastSent = p;
      lastSentAt = performance.now();
    };

    // At most ~60 pointer-move messages per second. Only the latest point
    // matters between animation frames; the backend preserves down/up order.
    const flushOnFrame = () => {
      raf = 0;
      if (activePointer === null || !live || !pending) return;
      if (performance.now() - lastSentAt >= 15) {
        sendMove(pending);
        pending = null;
      } else {
        raf = requestAnimationFrame(flushOnFrame);
      }
    };

    const finish = (e, cancelled = false) => {
      if (activePointer !== e.pointerId) return;
      e.preventDefault();
      const end = norm(e);
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      pending = null;

      if (live) {
        // Preserve the final position before mouse-up.
        if (dragging && (!lastSent ||
            Math.hypot(end.x - lastSent.x, end.y - lastSent.y) > 0.001)) {
          sendMove(end);
        }
        this.send({ type: 'touch_up', x: end.x, y: end.y });
      } else if (!cancelled && origin) {
        // Existing tap/drag protocol for non-Quartz backends.
        const dx = end.x - origin.x, dy = end.y - origin.y;
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD) {
          this.send({ type: 'tap', x: end.x, y: end.y });
        } else {
          this.send({ type: 'drag', x1: origin.x, y1: origin.y, x2: end.x, y2: end.y });
        }
      }

      // Clear before releasePointerCapture, which may fire lostpointercapture.
      activePointer = null;
      origin = null;
      live = false;
      dragging = false;
      lastSent = null;
      if (canvas.hasPointerCapture(e.pointerId)) {
        canvas.releasePointerCapture(e.pointerId);
      }
    };

    canvas.addEventListener('pointerdown', e => {
      if (activePointer !== null || e.isPrimary === false) return;
      e.preventDefault();
      canvas.setPointerCapture(e.pointerId);
      activePointer = e.pointerId;
      origin = norm(e);
      live = this.#liveTouch && this.#ws?.readyState === WebSocket.OPEN;
      dragging = false;
      pending = null;
      lastSentAt = 0;
      lastSent = origin;
      if (live) this.send({ type: 'touch_down', x: origin.x, y: origin.y });
    }, { passive: false });

    canvas.addEventListener('pointermove', e => {
      if (e.pointerId !== activePointer || !live || !origin) return;
      e.preventDefault();
      const p = norm(e);
      if (!dragging && Math.hypot(p.x - origin.x, p.y - origin.y) >= DRAG_THRESHOLD) {
        dragging = true;
      }
      if (!dragging) return;
      pending = p;
      if (!raf) raf = requestAnimationFrame(flushOnFrame);
    }, { passive: false });

    canvas.addEventListener('pointerup', e => finish(e), { passive: false });
    canvas.addEventListener('pointercancel', e => finish(e, true), { passive: false });
    canvas.addEventListener('lostpointercapture', e => {
      if (activePointer === e.pointerId) finish(e, true);
    });
  }
}
