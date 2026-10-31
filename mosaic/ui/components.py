"""Custom interface pieces built with Streamlit Components v2 (no build step, no extra package).

- crop_frame: the crop editor. Drag the frame to move it, drag a corner to resize it (the shape
  is kept), arrow keys nudge it (Shift = bigger steps). Only the final position is sent to Python
  when you let go, so dragging is smooth and never fights with the app.
- danger_button: hold-to-confirm button for actions that delete things.
"""
from __future__ import annotations

import streamlit as st

# --------------------------------------------------------------------------- crop editor
CROP_HTML = """
<div class="mpe-crop" tabindex="0" aria-label="Crop frame. Drag to move, drag a corner to resize, arrow keys to nudge.">
  <img class="mpe-img" draggable="false" alt="">
  <div class="mpe-photo"></div>
  <div class="mpe-frame">
    <div class="mpe-thirds"></div>
    <div class="mpe-eye"></div>
    <span class="mpe-h" data-h="nw"></span><span class="mpe-h" data-h="ne"></span>
    <span class="mpe-h" data-h="sw"></span><span class="mpe-h" data-h="se"></span>
  </div>
</div>
"""

CROP_CSS = """
.mpe-crop { position: relative; overflow: hidden; margin: 0 auto; user-select: none; -webkit-user-select: none;
  touch-action: none; outline: none; border-radius: 6px; background: var(--st-secondary-background-color); }
.mpe-crop:focus-visible { box-shadow: 0 0 0 3px color-mix(in srgb, var(--st-primary-color) 45%, transparent); }
.mpe-img { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.mpe-photo { position: absolute; pointer-events: none; outline: 1px dashed rgba(255,255,255,.7); }
.mpe-frame { position: absolute; cursor: move; box-sizing: border-box;
  border: 2px solid #fff; outline: 2px solid var(--st-primary-color);
  box-shadow: 0 0 0 9999px rgba(43, 20, 14, .45); }
.mpe-crop.dragging .mpe-frame { box-shadow: 0 0 0 9999px rgba(43, 20, 14, .3); }
.mpe-thirds { position: absolute; inset: 0; pointer-events: none; opacity: 0; transition: opacity .15s;
  background:
    linear-gradient(to right, transparent calc(33.333% - .5px), rgba(255,255,255,.55) calc(33.333% - .5px),
      rgba(255,255,255,.55) calc(33.333% + .5px), transparent calc(33.333% + .5px),
      transparent calc(66.666% - .5px), rgba(255,255,255,.55) calc(66.666% - .5px),
      rgba(255,255,255,.55) calc(66.666% + .5px), transparent calc(66.666% + .5px)),
    linear-gradient(to bottom, transparent calc(33.333% - .5px), rgba(255,255,255,.55) calc(33.333% - .5px),
      rgba(255,255,255,.55) calc(33.333% + .5px), transparent calc(33.333% + .5px),
      transparent calc(66.666% - .5px), rgba(255,255,255,.55) calc(66.666% - .5px),
      rgba(255,255,255,.55) calc(66.666% + .5px), transparent calc(66.666% + .5px)); }
.mpe-crop.dragging .mpe-thirds, .mpe-crop:hover .mpe-thirds { opacity: 1; }
.mpe-eye { position: absolute; left: 0; right: 0; height: 0; pointer-events: none; display: none;
  border-top: 1.5px dashed rgba(255,255,255,.85); }
.mpe-eye::after { content: "eye line"; position: absolute; right: 4px; top: -15px; font: 600 10px/1 var(--st-font);
  color: #fff; text-shadow: 0 1px 2px rgba(0,0,0,.6); letter-spacing: .03em; }
.mpe-h { position: absolute; width: 14px; height: 14px; background: #fff; border: 2px solid var(--st-primary-color);
  border-radius: 3px; box-sizing: border-box; }
.mpe-h[data-h="nw"] { left: -8px; top: -8px; cursor: nwse-resize; }
.mpe-h[data-h="ne"] { right: -8px; top: -8px; cursor: nesw-resize; }
.mpe-h[data-h="sw"] { left: -8px; bottom: -8px; cursor: nesw-resize; }
.mpe-h[data-h="se"] { right: -8px; bottom: -8px; cursor: nwse-resize; }
"""

CROP_JS = """
const MIN = 28;

function clampBox(b, W, H) {
  let [x0, y0, x1, y1] = b;
  const w = x1 - x0, h = y1 - y0;
  x0 = Math.min(Math.max(0, x0), W - w); y0 = Math.min(Math.max(0, y0), H - h);
  return [x0, y0, x0 + w, y0 + h];
}

function render(root, st) {
  const [x0, y0, x1, y1] = st.box;
  const f = root.querySelector('.mpe-frame').style;
  f.left = x0 + 'px'; f.top = y0 + 'px'; f.width = (x1 - x0) + 'px'; f.height = (y1 - y0) + 'px';
  const eye = root.querySelector('.mpe-eye');
  if (st.data.guide) { eye.style.display = 'block'; eye.style.top = (st.data.guide * (y1 - y0)) + 'px'; }
  else eye.style.display = 'none';
}

function commit(st) {
  st.seq = (st.seq || 0) + 1;
  st.pending = st.seq;
  st.setStateValue('box', { box: st.box.map(v => Math.round(v * 100) / 100), seq: st.seq });
}

function init(root, st) {
  let drag = null, keyTimer = null;
  const pt = e => { const r = root.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  const down = e => {
    if (e.button !== 0) return;
    const frame = root.querySelector('.mpe-frame');
    const h = e.target.dataset ? e.target.dataset.h : null;
    if (!h && !frame.contains(e.target)) return;
    e.preventDefault(); root.focus({ preventScroll: true });
    drag = { mode: h || 'move', p: pt(e), box: st.box.slice() };
    root.classList.add('dragging');
    root.setPointerCapture(e.pointerId);
  };
  const move = e => {
    if (!drag) return;
    const [px, py] = pt(e), W = st.data.w, H = st.data.h, ratio = st.data.ratio;
    let [x0, y0, x1, y1] = drag.box;
    if (drag.mode === 'move') {
      st.box = clampBox([x0 + px - drag.p[0], y0 + py - drag.p[1], x1 + px - drag.p[0], y1 + py - drag.p[1]], W, H);
    } else {
      const sx = drag.mode.includes('e') ? 1 : -1, sy = drag.mode.includes('s') ? 1 : -1;
      const ax = sx > 0 ? x0 : x1, ay = sy > 0 ? y0 : y1;            // the opposite corner stays put
      const maxW = Math.min(sx > 0 ? W - ax : ax, (sy > 0 ? H - ay : ay) * ratio);
      let w = Math.max((px - ax) * sx, (py - ay) * sy * ratio);
      w = Math.min(Math.max(w, MIN), maxW);
      const h = w / ratio;
      st.box = [sx > 0 ? ax : ax - w, sy > 0 ? ay : ay - h, sx > 0 ? ax + w : ax, sy > 0 ? ay + h : ay];
    }
    render(root, st);
  };
  const up = () => {
    if (!drag) return;
    const moved = st.box.some((v, i) => Math.abs(v - drag.box[i]) > 0.5);
    drag = null; root.classList.remove('dragging');
    if (moved) commit(st);
  };
  const key = e => {
    const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    if (!d) return;
    e.preventDefault();
    const s = e.shiftKey ? 10 : 1, [x0, y0, x1, y1] = st.box;
    st.box = clampBox([x0 + d[0] * s, y0 + d[1] * s, x1 + d[0] * s, y1 + d[1] * s], st.data.w, st.data.h);
    render(root, st);
    clearTimeout(keyTimer); keyTimer = setTimeout(() => commit(st), 350);
  };
  root.addEventListener('pointerdown', down);
  root.addEventListener('pointermove', move);
  root.addEventListener('pointerup', up);
  root.addEventListener('pointercancel', up);
  root.addEventListener('keydown', key);
  st.teardown = () => {
    clearTimeout(keyTimer);
    root.removeEventListener('pointerdown', down); root.removeEventListener('pointermove', move);
    root.removeEventListener('pointerup', up); root.removeEventListener('pointercancel', up);
    root.removeEventListener('keydown', key);
  };
}

export default function (component) {
  const { parentElement, data, setStateValue } = component;
  const root = parentElement.querySelector('.mpe-crop');
  if (!root || !data) return;
  let st = root.__mpe;
  if (!st) { st = root.__mpe = {}; init(root, st); }
  st.setStateValue = setStateValue;
  st.data = data;
  root.style.width = data.w + 'px'; root.style.height = data.h + 'px';
  const img = root.querySelector('.mpe-img');
  if (img.getAttribute('src') !== data.img) img.setAttribute('src', data.img);
  const ph = root.querySelector('.mpe-photo').style, [p0, p1, p2, p3] = data.photo;
  const padded = p0 > 0.5 || p1 > 0.5 || p2 < data.w - 0.5 || p3 < data.h - 0.5;
  ph.display = padded ? 'block' : 'none';
  ph.left = p0 + 'px'; ph.top = p1 + 'px'; ph.width = (p2 - p0) + 'px'; ph.height = (p3 - p1) + 'px';
  const incoming = JSON.stringify(data.box);
  const answered = st.pending && data.seq >= st.pending;
  if (answered) st.pending = null;
  if (incoming !== st.lastIncoming || answered || !st.box) {
    st.lastIncoming = incoming;
    st.box = data.box.slice();
  }
  render(root, st);
  return () => st.teardown && st.teardown();
}
"""

_crop_component = st.components.v2.component("mpe_crop_frame", html=CROP_HTML, css=CROP_CSS, js=CROP_JS)


def crop_frame(key: str, img_data_url: str, size, box, ratio: float, photo, guide: float | None,
               seq: int, on_change):
    """Render the crop editor. box/photo are (x0, y0, x1, y1) in display pixels.
    on_change runs (before the rerun) when the user lets go of the frame; read the new box with
    crop_frame_value(key)."""
    w, h = size
    return _crop_component(
        key=key, width="stretch", on_box_change=on_change,
        data={"img": img_data_url, "w": w, "h": h, "box": [float(v) for v in box], "ratio": float(ratio),
              "photo": [float(v) for v in photo], "guide": guide, "seq": seq})


def crop_frame_value(key: str):
    """The last box the user set: {"box": [...], "seq": n} or None."""
    state = st.session_state.get(key)
    if state is None:
        return None
    val = state.get("box") if hasattr(state, "get") else getattr(state, "box", None)
    return val if isinstance(val, dict) and "box" in val else None


# --------------------------------------------------------------------------- hold-to-confirm button
DANGER_HTML = """
<button class="mpe-danger" type="button">
  <span class="mpe-fill"></span>
  <svg class="mpe-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>
  <span class="mpe-label"></span>
</button>
<div class="mpe-sub"></div>
"""

DANGER_CSS = """
.mpe-danger { position: relative; overflow: hidden; width: 100%; min-height: 2.6rem; padding: .45rem .9rem;
  display: flex; align-items: center; justify-content: center; gap: .5rem; cursor: pointer;
  border-radius: var(--st-button-radius, .6rem); border: 1px solid var(--st-red-color);
  background: var(--st-background-color); color: var(--st-red-color);
  font: 600 .875rem/1.2 var(--st-font); transition: transform .15s ease, background .2s ease; }
.mpe-danger:hover { background: var(--st-red-background-color); }
.mpe-danger:active:not(:disabled) { transform: scale(.985); }
.mpe-danger:disabled { cursor: not-allowed; opacity: .55; }
.mpe-danger.holding { animation: mpe-pulse .6s ease-in-out infinite; }
.mpe-danger.done { background: var(--st-red-color); color: #fff; }
.mpe-fill { position: absolute; inset: 0; width: 0; background: var(--st-red-color); opacity: .22; pointer-events: none; }
.mpe-icon { width: 1.05rem; height: 1.05rem; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round;
  stroke-linejoin: round; position: relative; }
.mpe-label { position: relative; }
.mpe-sub { margin-top: .3rem; font: .75rem/1.35 var(--st-font); color: var(--st-text-color); opacity: .65; }
@keyframes mpe-pulse { 0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--st-red-color) 45%, transparent); }
  50% { box-shadow: 0 0 0 6px transparent; } }
"""

DANGER_JS = """
export default function ({ parentElement, setTriggerValue, data }) {
  const HOLD = (data && data.hold_ms) || 1500;
  const btn = parentElement.querySelector('.mpe-danger');
  const fill = parentElement.querySelector('.mpe-fill');
  const label = parentElement.querySelector('.mpe-label');
  const sub = parentElement.querySelector('.mpe-sub');
  const idle = (data && data.label) || 'Hold to delete';
  label.textContent = idle;
  sub.textContent = (data && data.help) || '';
  btn.disabled = !!(data && data.disabled);
  if (btn.__mpeBound) return;
  btn.__mpeBound = true;
  let start = null, raf = null, cool = false;
  const tick = () => {
    if (!start) return;
    const p = Math.min((Date.now() - start) / HOLD, 1);
    fill.style.width = (p * 100) + '%';
    if (p >= 1) fire(); else raf = requestAnimationFrame(tick);
  };
  const begin = e => {
    if (cool || btn.disabled) return;
    if (e && e.cancelable) e.preventDefault();
    start = Date.now(); btn.classList.add('holding');
    label.textContent = (btn.__data && btn.__data.holding) || 'Keep holding...';
    raf = requestAnimationFrame(tick);
  };
  const cancel = () => {
    if (cool) return;
    start = null; btn.classList.remove('holding'); fill.style.width = '0';
    label.textContent = (btn.__data && btn.__data.label) || idle;
    if (raf) cancelAnimationFrame(raf);
  };
  const fire = () => {
    cancelAnimationFrame(raf); start = null; cool = true;
    btn.classList.remove('holding'); btn.classList.add('done');
    label.textContent = (btn.__data && btn.__data.done) || 'Done';
    setTriggerValue('confirmed', true);
    setTimeout(() => { cool = false; btn.classList.remove('done'); fill.style.width = '0';
      label.textContent = (btn.__data && btn.__data.label) || idle; }, 1600);
  };
  btn.addEventListener('mousedown', begin);
  btn.addEventListener('mouseup', cancel);
  btn.addEventListener('mouseleave', cancel);
  btn.addEventListener('touchstart', begin, { passive: false });
  btn.addEventListener('touchend', cancel);
  btn.addEventListener('touchcancel', cancel);
  btn.addEventListener('keydown', e => { if ((e.key === ' ' || e.key === 'Enter') && !start) begin(e); });
  btn.addEventListener('keyup', e => { if (e.key === ' ' || e.key === 'Enter') cancel(); });
}
"""

DANGER_JS = DANGER_JS.replace(
    "  btn.disabled = !!(data && data.disabled);\n",
    "  btn.disabled = !!(data && data.disabled);\n  btn.__data = data;\n")

_danger_component = st.components.v2.component("mpe_danger_button", html=DANGER_HTML, css=DANGER_CSS, js=DANGER_JS)


def danger_button(key: str, label: str, on_confirm, help: str = "", done: str = "Done",
                  disabled: bool = False, hold_ms: int = 1500):
    """Hold-to-confirm button. on_confirm runs once the button has been held for hold_ms."""
    return _danger_component(
        key=key, width="stretch", on_confirmed_change=on_confirm,
        data={"label": label, "holding": "Keep holding...", "done": done, "help": help,
              "disabled": disabled, "hold_ms": hold_ms})
