'use strict';

const $ = (s) => document.querySelector(s);
const canvas = $('#screen');
const ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });
const overlay = $('#overlay');
const hint = $('#hint');
const wsUrl = (p) => (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + p;

function showOverlay(text) {
  overlay.textContent = text || '';
  overlay.hidden = !text;
}

function setPill(el, cls, title) {
  el.className = 'pill' + (cls ? ' ' + cls : '');
  if (title !== undefined) el.title = title;
}

async function checkAuth() {
  try {
    const r = await fetch('/api/state');
    if (r.status === 401) location.href = '/login';
  } catch { /* 伺服器暫時離線 */ }
}

// ---------- 版面 ----------

function fit() {
  const s = $('#stage');
  const k = Math.min(s.clientWidth / canvas.width, s.clientHeight / canvas.height);
  canvas.style.width = Math.floor(canvas.width * k) + 'px';
  canvas.style.height = Math.floor(canvas.height * k) + 'px';
}
new ResizeObserver(fit).observe($('#stage'));

// ---------- 影像 (H.264 Annex-B over WebSocket → WebCodecs) ----------

let decoder = null;
let codec = null;
let waitKey = true;
let frames = 0;
let bytes = 0;

function avcCodec(au) {
  for (let i = 0; i + 6 < au.length; i++) {
    if (au[i] === 0 && au[i + 1] === 0 && au[i + 2] === 1 && (au[i + 3] & 0x1f) === 7) {
      return 'avc1.' + [au[i + 4], au[i + 5], au[i + 6]]
        .map((b) => b.toString(16).padStart(2, '0')).join('');
    }
  }
  return null;
}

function resetDecoder() {
  if (decoder && decoder.state !== 'closed') decoder.close();
  decoder = null;
  codec = null;
  waitKey = true;
}

function newDecoder(c) {
  const d = new VideoDecoder({
    output: (f) => {
      if (canvas.width !== f.displayWidth || canvas.height !== f.displayHeight) {
        canvas.width = f.displayWidth;
        canvas.height = f.displayHeight;
        fit();
      }
      ctx.drawImage(f, 0, 0);
      f.close();
      frames++;
    },
    error: (e) => {
      console.warn('decoder error', e);
      resetDecoder();
    },
  });
  d.configure({ codec: c, optimizeForLatency: true });
  return d;
}

function onVideoData(buf) {
  const u8 = new Uint8Array(buf);
  const key = (u8[0] & 1) === 1;
  const ts = Number(new DataView(buf).getBigUint64(1));
  const data = u8.subarray(9);
  bytes += buf.byteLength;

  if (key) {
    const c = avcCodec(data);
    if (c && (c !== codec || !decoder)) {
      resetDecoder();
      try {
        decoder = newDecoder(c);
        codec = c;
      } catch (e) {
        showOverlay('瀏覽器無法解碼此影像格式: ' + c);
        return;
      }
    }
    waitKey = false;
  }
  if (waitKey || !decoder) return;
  if (!key && decoder.decodeQueueSize > 8) {
    waitKey = true; // 解碼跟不上，跳到下一個關鍵影格
    return;
  }
  decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: ts, data }));
  if (!overlay.hidden) showOverlay('');
}

function connectVideo() {
  if (!('VideoDecoder' in window)) {
    showOverlay(window.isSecureContext
      ? '此瀏覽器不支援 WebCodecs，請使用新版 Chrome / Edge / Firefox / Safari'
      : '需要以 HTTPS 開啟才能解碼影像');
    setPill($('#st-video'), 'bad');
    return;
  }
  const ws = new WebSocket(wsUrl('/ws/video'));
  ws.binaryType = 'arraybuffer';
  showOverlay('連線中…');
  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') {
      onVideoData(ev.data);
      return;
    }
    const m = JSON.parse(ev.data);
    if (m.t === 'info') {
      resetDecoder();
      showOverlay('等待畫面…');
      setPill($('#st-video'), 'ok', `${m.width}x${m.height}@${m.fps} · ${m.encoder}`);
    } else if (m.t === 'error') {
      showOverlay('影像擷取錯誤：\n' + m.msg);
      setPill($('#st-video'), 'bad', m.msg);
    }
  };
  ws.onclose = () => {
    resetDecoder();
    setPill($('#st-video'), 'bad');
    showOverlay('影像連線中斷，重新連線中…');
    checkAuth();
    setTimeout(connectVideo, 2000);
  };
}

setInterval(() => {
  $('#stats').textContent = `${frames} fps · ${(bytes * 8 / 1e6).toFixed(1)} Mbps`;
  frames = 0;
  bytes = 0;
}, 1000);

// ---------- HID ----------

let hws = null;

function send(m) {
  if (hws && hws.readyState === WebSocket.OPEN) hws.send(JSON.stringify(m));
}

function renderStatus(s) {
  setPill($('#st-hid'), s && s.chip ? 'ok' : 'bad',
    s ? `${s.port || '無序列埠'} ${s.version || ''}` : '未連線');
  setPill($('#st-usb'), s && s.chip ? (s.usb ? 'ok' : 'warn') : '');
  for (const k of ['num', 'caps', 'scroll']) $('#led-' + k).classList.toggle('on', !!(s && s[k]));
}

function connectHid() {
  hws = new WebSocket(wsUrl('/ws/hid'));
  hws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === 'status') renderStatus(m);
    else if (m.t === 'typed' && m.skipped) flashHint(`有 ${m.skipped} 個非 ASCII 字元無法輸入，已略過`);
  };
  hws.onclose = () => {
    renderStatus(null);
    checkAuth();
    setTimeout(connectHid, 2000);
  };
}

// KeyboardEvent.code → USB HID usage
const KEYMAP = (() => {
  const m = {};
  for (let i = 0; i < 26; i++) m['Key' + String.fromCharCode(65 + i)] = 0x04 + i;
  for (let i = 1; i <= 9; i++) m['Digit' + i] = 0x1d + i;
  m.Digit0 = 0x27;
  for (let i = 1; i <= 12; i++) m['F' + i] = 0x39 + i;
  for (let i = 13; i <= 24; i++) m['F' + i] = 0x68 + i - 13;
  for (let i = 1; i <= 9; i++) m['Numpad' + i] = 0x58 + i;
  m.Numpad0 = 0x62;
  Object.assign(m, {
    Enter: 0x28, Escape: 0x29, Backspace: 0x2a, Tab: 0x2b, Space: 0x2c,
    Minus: 0x2d, Equal: 0x2e, BracketLeft: 0x2f, BracketRight: 0x30, Backslash: 0x31,
    IntlHash: 0x32, Semicolon: 0x33, Quote: 0x34, Backquote: 0x35, Comma: 0x36,
    Period: 0x37, Slash: 0x38, CapsLock: 0x39,
    PrintScreen: 0x46, ScrollLock: 0x47, Pause: 0x48, Insert: 0x49, Home: 0x4a,
    PageUp: 0x4b, Delete: 0x4c, End: 0x4d, PageDown: 0x4e,
    ArrowRight: 0x4f, ArrowLeft: 0x50, ArrowDown: 0x51, ArrowUp: 0x52,
    NumLock: 0x53, NumpadDivide: 0x54, NumpadMultiply: 0x55, NumpadSubtract: 0x56,
    NumpadAdd: 0x57, NumpadEnter: 0x58, NumpadDecimal: 0x63, NumpadEqual: 0x67,
    IntlBackslash: 0x64, ContextMenu: 0x65, Power: 0x66,
    IntlRo: 0x87, KanaMode: 0x88, IntlYen: 0x89, Convert: 0x8a, NonConvert: 0x8b,
    Lang1: 0x90, Lang2: 0x91,
    ControlLeft: 0xe0, ShiftLeft: 0xe1, AltLeft: 0xe2, MetaLeft: 0xe3,
    ControlRight: 0xe4, ShiftRight: 0xe5, AltRight: 0xe6, MetaRight: 0xe7,
  });
  return m;
})();

const pressed = new Set();

canvas.addEventListener('keydown', (e) => {
  const u = KEYMAP[e.code];
  if (u === undefined) return;
  e.preventDefault();
  if (pressed.has(u)) return;
  pressed.add(u);
  send({ t: 'kd', c: u });
});

canvas.addEventListener('keyup', (e) => {
  const u = KEYMAP[e.code];
  if (u === undefined) return;
  e.preventDefault();
  pressed.delete(u);
  send({ t: 'ku', c: u });
});

function releaseAll() {
  pressed.clear();
  buttons = 0;
  send({ t: 'release' });
}

canvas.addEventListener('focus', () => {
  canvas.classList.add('captured');
  hint.hidden = true;
});
canvas.addEventListener('blur', () => {
  canvas.classList.remove('captured');
  releaseAll();
  showHint();
});
document.addEventListener('visibilitychange', () => { if (document.hidden) releaseAll(); });

// ---------- 滑鼠 ----------

let mouseMode = 'abs';
let buttons = 0;
let pendingAbs = null;
let relX = 0;
let relY = 0;
let wheelAcc = 0;
let rafQueued = false;

function absPos(e) {
  const r = canvas.getBoundingClientRect();
  const clamp = (v) => Math.min(Math.max(v, 0), 1);
  return {
    x: Math.round(clamp((e.clientX - r.left) / r.width) * 4095),
    y: Math.round(clamp((e.clientY - r.top) / r.height) * 4095),
  };
}

function flushMouse() {
  rafQueued = false;
  if (pendingAbs) {
    send({ t: 'ma', x: pendingAbs.x, y: pendingAbs.y, b: buttons });
    pendingAbs = null;
  }
  if (relX || relY) {
    send({ t: 'mr', dx: relX, dy: relY, b: buttons });
    relX = relY = 0;
  }
}

function scheduleMouse() {
  if (!rafQueued) {
    rafQueued = true;
    requestAnimationFrame(flushMouse);
  }
}

const relLocked = () => document.pointerLockElement === canvas;

function sendButtons(e) {
  buttons = e.buttons & 7; // 瀏覽器與 HID 的按鍵位元相同：左=1 右=2 中=4
  flushMouse();
  if (mouseMode === 'abs') {
    const p = absPos(e);
    send({ t: 'ma', x: p.x, y: p.y, b: buttons });
  } else {
    send({ t: 'mr', dx: 0, dy: 0, b: buttons });
  }
}

canvas.addEventListener('pointerdown', (e) => {
  canvas.focus();
  if (mouseMode === 'rel' && !relLocked()) {
    canvas.requestPointerLock();
    return;
  }
  e.preventDefault();
  canvas.setPointerCapture(e.pointerId);
  sendButtons(e);
});

canvas.addEventListener('pointerup', (e) => {
  if (mouseMode === 'rel' && !relLocked()) return;
  e.preventDefault();
  sendButtons(e);
});

canvas.addEventListener('pointermove', (e) => {
  if (mouseMode === 'rel' && !relLocked()) return;
  if ((e.buttons & 7) !== buttons) { // 同時按多個鍵時只會觸發 pointermove
    sendButtons(e);
    return;
  }
  if (mouseMode === 'abs') {
    pendingAbs = absPos(e);
  } else {
    relX += e.movementX;
    relY += e.movementY;
  }
  scheduleMouse();
});

canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const px = e.deltaMode === 1 ? e.deltaY * 40 : e.deltaMode === 2 ? e.deltaY * 800 : e.deltaY;
  wheelAcc += px;
  let n = Math.trunc(wheelAcc / 100);
  if (!n) return;
  wheelAcc -= n * 100;
  n = Math.max(-10, Math.min(10, n));
  send({ t: 'mw', d: -n }); // HID 正值為向上
}, { passive: false });

canvas.addEventListener('contextmenu', (e) => e.preventDefault());

document.addEventListener('pointerlockchange', () => {
  if (!relLocked() && buttons) {
    buttons = 0;
    send({ t: 'mr', dx: 0, dy: 0, b: 0 });
  }
  showHint();
});

// ---------- 提示 ----------

let hintTimer = null;

function showHint() {
  if (document.activeElement !== canvas) {
    hint.textContent = '點擊畫面開始操作鍵盤與滑鼠';
  } else if (mouseMode === 'rel' && !relLocked()) {
    hint.textContent = '相對滑鼠模式：點擊畫面鎖定游標，按 Esc 解除';
  } else {
    hint.hidden = true;
    return;
  }
  hint.hidden = false;
}

function flashHint(text) {
  hint.textContent = text;
  hint.hidden = false;
  clearTimeout(hintTimer);
  hintTimer = setTimeout(showHint, 4000);
}

// ---------- 工具列 ----------

let state = null;

async function loadState() {
  const r = await fetch('/api/state');
  if (r.status === 401) {
    location.href = '/login';
    return;
  }
  state = await r.json();
  const v = state.video;

  const dev = $('#sel-device');
  dev.replaceChildren(new Option('自動選擇擷取卡', 'auto'));
  for (const d of state.devices) dev.add(new Option(d.name, d.path));
  dev.value = v.device;

  const mode = $('#sel-mode');
  mode.replaceChildren();
  const modes = state.modes.length ? state.modes : [{ width: v.width, height: v.height, fps: [v.fps] }];
  for (const m of modes) {
    for (const f of m.fps) mode.add(new Option(`${m.width}×${m.height} @ ${f}`, `${m.width}x${m.height}@${f}`));
  }
  mode.value = `${v.width}x${v.height}@${v.fps}`;

  const br = $('#sel-bitrate');
  if (![...br.options].some((o) => +o.value === v.bitrate)) br.add(new Option(`${v.bitrate / 1000} Mbps`, v.bitrate));
  br.value = v.bitrate;
  renderStatus(state.hid);
}

async function applyVideo() {
  const [wh, fps] = $('#sel-mode').value.split('@');
  const [width, height] = wh.split('x').map(Number);
  const r = await fetch('/api/video', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      device: $('#sel-device').value, width, height, fps: +fps, bitrate: +$('#sel-bitrate').value,
    }),
  });
  if (!r.ok) flashHint('設定失敗：' + await r.text());
  await loadState();
}

$('#sel-device').addEventListener('change', async () => {
  // 換裝置後支援的模式可能不同，先套用再重新載入模式清單
  await applyVideo();
});
$('#sel-mode').addEventListener('change', applyVideo);
$('#sel-bitrate').addEventListener('change', applyVideo);

$('#sel-mouse').addEventListener('change', (e) => {
  mouseMode = e.target.value;
  if (mouseMode === 'abs' && relLocked()) document.exitPointerLock();
  canvas.style.cursor = mouseMode === 'abs' ? '' : 'default';
  canvas.focus();
  showHint();
});

$('#btn-cad').addEventListener('click', () => {
  send({ t: 'combo', c: [0xe0, 0xe2, 0x4c] });
  canvas.focus();
});

$('#btn-release').addEventListener('click', () => {
  releaseAll();
  canvas.focus();
});

$('#btn-paste').addEventListener('click', () => {
  $('#paste-text').value = '';
  $('#dlg-paste').showModal();
});

$('#dlg-paste').addEventListener('close', () => {
  const dlg = $('#dlg-paste');
  if (dlg.returnValue === 'send' && $('#paste-text').value) send({ t: 'txt', s: $('#paste-text').value });
  canvas.focus();
});

$('#btn-fs').addEventListener('click', async () => {
  await $('#stage').requestFullscreen();
  // 全螢幕時鎖定鍵盤，讓 Esc、Alt+Tab、Win 等按鍵也能送到被控端（Chromium 支援）
  if (navigator.keyboard && navigator.keyboard.lock) navigator.keyboard.lock().catch(() => {});
  canvas.focus();
});

document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement && navigator.keyboard && navigator.keyboard.unlock) navigator.keyboard.unlock();
});

// ---------- 啟動 ----------

fit();
showHint();
loadState().catch(() => {});
connectVideo();
connectHid();
