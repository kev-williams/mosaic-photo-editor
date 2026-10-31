"""Shared interface code: styles, settings access, crop editing, previews, thumbnails, summary."""
from __future__ import annotations

import base64
import html
import io
import os
import uuid
from dataclasses import replace

import numpy as np
import streamlit as st
from PIL import Image

from .. import ai, core, history
from ..core import Output, Settings, Source
from ..paths import ADJ_FILE, read_json, write_json
from .components import crop_frame, crop_frame_value

PRIMARY = "#C8343E"       # Persian / candy-apple red
DEEP = "#8E2430"          # headings
GRADIENT = "linear-gradient(120deg, #B8263A 0%, #D9453B 45%, #E06A35 100%)"   # red to sienna orange
PEACH = "#FFF1E9"
EDIT_MAX = (520, 560)    # px, crop editor canvas
WORK_DRAFT = 2400        # px, decode size used while editing (enough for every preset)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def session_id() -> str:
    if "_sid" not in st.session_state:
        st.session_state["_sid"] = uuid.uuid4().hex[:12]
    return st.session_state["_sid"]


def styles():
    from .. import native
    # in the app window / on a Mac, the Browse button replaces the uploader's own button
    hide_upload_button = ('.st-key-up_files [data-testid="stFileUploaderDropzone"] button { display: none; }'
                          if native.can_pick() else "")
    st.markdown(f"""
<style>
  /* keep the page clear of the top navigation bar */
  .block-container, [data-testid="stMainBlockContainer"] {{ padding-top: 5rem; max-width: 1500px; }}
  /* larger logo and name, top left */
  [data-testid="stSidebarLogo"], [data-testid="stHeaderLogo"] {{ height: 3.1rem; max-width: 15.5rem; }}
  [data-testid="stSidebarHeader"] {{ padding-top: 1rem; padding-bottom: 1rem; height: auto; }}
  /* big drop area for photos */
  .st-key-up_files [data-testid="stFileUploaderDropzone"] {{ min-height: 11rem; justify-content: center;
      border: 2px dashed #E9B7A3; background: #FFF7F2; }}
  .st-key-up_files [data-testid="stFileUploaderDropzone"]:hover {{ border-color: {PRIMARY}; background: #FFF1E9; }}
  .st-key-up_files [data-testid="stFileUploaderDropzone"] > div {{ width: 100%; text-align: center; }}
  {hide_upload_button}
  .mpe-hint {{ display: inline-flex; align-items: center; justify-content: center; width: 1.15rem; height: 1.15rem;
      border-radius: 50%; border: 1.5px solid #B9978C; color: #8E6A60; font-size: .72rem; font-weight: 700;
      cursor: help; user-select: none; }}
  h1, h2, h3 {{ letter-spacing: -0.01em; }}
  [data-testid="stMain"] h3 {{ color: {DEEP}; }}
  [data-testid="stMetricValue"] {{ color: {PRIMARY}; }}
  /* sidebar section boxes */
  [data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"] {{ background: #FFFFFF; }}
  [data-testid="stSidebar"] .mpe-sec {{ display: flex; align-items: center; gap: .45rem; margin: 0 0 .15rem;
      font-weight: 700; font-size: .78rem; letter-spacing: .08em; text-transform: uppercase; color: {DEEP}; }}
  [data-testid="stSidebar"] .mpe-sec::before {{ content: ""; width: .55rem; height: .55rem; border-radius: 2px;
      background: {GRADIENT}; transform: rotate(45deg); }}
  [data-testid="stSidebar"] .mpe-sub {{ font-weight: 600; font-size: .85rem; margin: .35rem 0 -.2rem; color: #5A2A1E; }}
  /* the Apply bar sticks to the bottom of the sidebar */
  [data-testid="stLayoutWrapper"]:has(> .st-key-apply-bar) {{ position: sticky; bottom: 0; z-index: 5; }}
  .st-key-apply-bar {{ padding: .7rem .1rem .6rem;
      background: linear-gradient(to top, #FFF5EF 80%, rgba(255,245,239,0)); }}
  .st-key-apply-bar button[kind="primary"] {{ background: {GRADIENT}; border: 0; }}
  .st-key-apply-bar button[kind="primary"]:disabled {{ background: #F2CFC0; color: #fff; }}
  /* previews: 1px outline so white backgrounds still show the frame edge (display only) */
  .mpe-preview {{ display: flex; align-items: center; justify-content: center; border-radius: 6px;
      background: {PEACH}; overflow: hidden; }}
  .mpe-preview img {{ max-width: 100%; max-height: 100%; box-shadow: 0 0 0 1px #A8A8A8; }}
  [data-testid="stMain"] [data-testid="stImage"] img {{ box-shadow: 0 0 0 1px #E3C3B4; }}
  .thumbcap {{ font-size: .75rem; line-height: 1.3; margin-top: -.6rem; }}
  .thumbcap div {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .thumbcap .t {{ font-weight: 600; }}
  .thumbcap .l {{ color: #7A6560; }}
  .thumbcap .warn {{ color: #B45309; font-weight: 600; }}
  .thumbcap .manual {{ color: {PRIMARY}; font-weight: 600; }}
  .mpe-seen {{ display: inline-flex; align-items: center; justify-content: center; width: 1.5rem; height: 1.5rem;
      border-radius: 50%; background: #D97706; color: #fff; font-weight: 800; font-size: .95rem; cursor: help;
      box-shadow: 0 0 0 3px rgba(217,119,6,.22); user-select: none; }}
  .mpe-seen-row {{ display: flex; align-items: center; gap: .5rem; font-size: .8rem; color: #8A5A00; }}
  .mpe-info {{ font-size: .88rem; line-height: 1.5; }}
  .mpe-info b {{ color: {DEEP}; }}
</style>
""", unsafe_allow_html=True)


def section(title: str):
    st.markdown(f'<div class="mpe-sec">{html.escape(title)}</div>', unsafe_allow_html=True)


def hint(text: str):
    """Small '?' that shows text on hover (a plain browser tooltip, so it never gets stuck)."""
    st.markdown(f'<span class="mpe-hint" title="{html.escape(text, quote=True)}">?</span>',
                unsafe_allow_html=True, width="content")


def subhead(title: str):
    st.markdown(f'<div class="mpe-sub">{html.escape(title)}</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------- settings
def current_settings() -> Settings:
    """Applied settings from the sidebar plus the output format chosen on the page."""
    s = st.session_state.get("applied") or Settings()
    return replace(s, fmt=st.session_state.get("fmt_value", "webp"))


def mask_fn_for(s: Settings):
    return ai.mask_for if s.white_bg else None


def extend_fn_for(s: Settings):
    return ai.extended if (s.ai_extend and not s.white_bg) else None


def fill_pads(size, s: Settings):
    """How far crops may go beyond the photo with the current settings."""
    return core.pads(size, s) if (s.white_bg or s.ai_extend) else (0, 0, 0)


def has_effect(s: Settings) -> bool:
    return bool(s.white_bg or s.ai_extend)


def progress_note(s: Settings, text: str) -> str:
    """Progress text, saying so when the AI models are still loading (first use after starting)."""
    if has_effect(s) and ai.warm_state() in ("idle", "loading"):
        return text + "  ·  loading the AI models, the first photo takes a little longer"
    return text


# --------------------------------------------------------------------------- decoding (cached)
@st.cache_resource(max_entries=3, show_spinner=False)
def work_image(item_id: str, mkey, _src):
    """Reduced decode used while editing one photo; shared by the editor, previews and output."""
    return core.load(_src, draft=WORK_DRAFT)


@st.cache_data(max_entries=300, ttl=3600, show_spinner=False)
def cached_crop(item_id: str, mkey, _src, filename: str, s: Settings, base: str, box):
    loaded = work_image(item_id, mkey, _src)
    return core.make_crop(_src, filename, s, base, box, file_size(_src), mask_fn_for(s), f"{item_id}|{mkey}",
                          extend_fn_for(s), loaded)


def file_size(src) -> int:
    return len(src) if isinstance(src, (bytes, bytearray)) else os.path.getsize(src)


@st.cache_data(max_entries=2000, show_spinner=False)
def fingerprint(item_id: str, mkey, _src) -> str:
    try:
        return core.fingerprint(_src)
    except OSError:
        return ""


# --------------------------------------------------------------------------- crop adjustments
def adjustments() -> dict:
    """Hand-made crops: key -> [left, top, right, bottom] in full-resolution pixels.
    Adjustments of photos on disk are saved so they survive restarts."""
    if "adj" not in st.session_state:
        st.session_state.adj = read_json(ADJ_FILE, {})
    return st.session_state.adj


def save_adjustments():
    keep = {k: v for k, v in st.session_state.get("adj", {}).items() if k.startswith("/")}
    write_json(ADJ_FILE, keep)


def adj_key(src_id: str, dims, ratio) -> str:
    return f"{src_id}|{dims[0]}x{dims[1]}|{ratio[0]}:{ratio[1]}"


NUDGE = 0.02      # move step, share of crop height
ZOOM = 0.96       # zoom-in factor per click


def _nudge(key, action, auto, size, ratio, pad):
    store = adjustments()
    if action == "reset":
        store.pop(key, None)
    else:
        l, t, r, b = store.get(key) or auto
        w, h = r - l, b - t
        step = NUDGE * h
        if action == "more":
            t, b = t - step, b - step
        elif action == "less":
            t, b = t + step, b + step
        elif action == "left":
            l, r = l - step, r - step
        elif action == "right":
            l, r = l + step, r + step
        elif action in ("in", "out"):
            f = ZOOM if action == "in" else 1 / ZOOM
            cx, cy = (l + r) / 2, (t + b) / 2
            l, r, t, b = cx - w * f / 2, cx + w * f / 2, cy - h * f / 2, cy + h * f / 2
        store[key] = list(core.fit_ratio((l, t, r, b), ratio, size, pad))
    save_adjustments()


def data_url(img: Image.Image, fmt="JPEG", quality=88) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, fmt, quality=quality)
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()


@st.cache_data(max_entries=12, ttl=3600, show_spinner=False)
def editor_canvas(item_id: str, mkey, _src, size, pad, fill):
    """Editor picture: the photo plus any area the crop may use beyond it (shown in the background
    colour, or as a checkerboard where the AI extender will generate). Returns (data URL, (w, h), k,
    photo rect) with k = display pixels per photo pixel."""
    img = work_image(item_id, mkey, _src)[0]
    ptop, pside, pbot = core.pad3(pad)
    W, H = size
    cw, ch = W + 2 * pside, H + ptop + pbot
    k = min(EDIT_MAX[0] / cw, EDIT_MAX[1] / ch)
    dw, dh = max(1, round(cw * k)), max(1, round(ch * k))
    photo = img.resize((max(1, round(W * k)), max(1, round(H * k))), Image.LANCZOS).convert("RGB")
    if (ptop, pside, pbot) != (0, 0, 0):
        canvas = Image.new("RGB", (dw, dh), fill) if fill else core.checkerboard((dw, dh))
        canvas.paste(photo, (round(pside * k), round(ptop * k)))
    else:
        canvas = photo
    rect = (pside * k, ptop * k, (pside + W) * k, (ptop + H) * k)
    return data_url(canvas), (dw, dh), k, rect, photo


def crop_editor(key: str, item_id: str, mkey, src, size, auto, ratio, pad, fill, guide):
    """Drag-to-crop frame plus one-click buttons. Changes are stored in adjustments() and take
    effect straight away (this runs inside a fragment, so only the editor area refreshes)."""
    ptop, pside, pbot = core.pad3(pad)
    store = adjustments()
    url, (dw, dh), k, rect, proxy = editor_canvas(item_id, mkey, src, size, pad, fill)
    st.session_state["eyedrop_img"] = proxy
    box = store.get(key) or auto
    disp = ((box[0] + pside) * k, (box[1] + ptop) * k, (box[2] + pside) * k, (box[3] + ptop) * k)
    seq_key = f"cropseq-{key}"
    fkey = f"crop-{abs(hash(key)) % 10**10}"

    def on_change():
        val = crop_frame_value(fkey)
        if not val or val.get("seq") == st.session_state.get(seq_key):
            return
        st.session_state[seq_key] = val.get("seq")
        x0, y0, x1, y1 = val["box"]
        raw = (x0 / k - pside, y0 / k - ptop, x1 / k - pside, y1 / k - ptop)
        adjustments()[key] = list(core.fit_ratio(raw, ratio, size, pad))
        save_adjustments()

    crop_frame(fkey, url, (dw, dh), disp, ratio[0] / ratio[1], rect, guide,
               st.session_state.get(seq_key) or 0, on_change)
    args = lambda act: dict(on_click=_nudge, args=(key, act, auto, size, ratio, pad), width="stretch")
    r1 = st.columns(4, gap="small")
    r1[0].button("Headroom +", key=f"more-{key}", **args("more"))
    r1[1].button("Headroom -", key=f"less-{key}", **args("less"))
    r1[2].button("Zoom in", key=f"in-{key}", **args("in"))
    r1[3].button("Zoom out", key=f"out-{key}", **args("out"))
    r2 = st.columns(4, gap="small")
    r2[0].button("Move left", key=f"left-{key}", **args("left"))
    r2[1].button("Move right", key=f"right-{key}", **args("right"))
    r2[2].button("Reset", key=f"reset-{key}", disabled=key not in store, **args("reset"))
    r2[3].caption(f"Crop: **{'manual' if key in store else 'auto'}**")
    return dh


def preview_html(data: bytes, mime: str, height: int) -> str:
    b64 = base64.b64encode(data).decode()
    return (f'<div class="mpe-preview" style="height:{height}px">'
            f'<img src="data:{mime};base64,{b64}" alt="Result preview"></div>')


@st.cache_data(max_entries=20, ttl=3600, show_spinner=False)
def before_image(item_id: str, mkey, _src, s: Settings, box) -> bytes:
    """The same crop without background replacement or AI (extension areas as a checkerboard),
    for the before / after comparison."""
    img, _, _ = work_image(item_id, mkey, _src)
    full = core.probe(_src)
    k = img.width / full[0]
    target = core.scaled_size((box[2] - box[0], box[3] - box[1]), s.crop.max_size)
    out = core.render_crop(img, tuple(v * k for v in box), s, target, False, effects=False, fill="checker")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def seen_badge(row: dict | None):
    """Exclamation mark with a hover note when the photo was already processed recently."""
    if not row:
        return
    tip = html.escape(history.describe(row), quote=True)
    st.markdown(f'<div class="mpe-seen-row"><span class="mpe-seen" title="{tip}">!</span>'
                f'<span title="{tip}">Processed before</span></div>', unsafe_allow_html=True)


def hist_row(action: str, src: Source, fp: str, o: Output, s: Settings, path: str | None = None) -> dict:
    return dict(session=session_id(), action=action, source_name=src.filename, source_fp=fp,
                source_bytes=src.nbytes, source_width=(src.dims or (None, None))[0],
                source_height=(src.dims or (None, None))[1], spec=s.spec, output_name=o.filename,
                output_format=s.fmt, output_width=(o.out_size or (None, None))[0],
                output_height=(o.out_size or (None, None))[1], output_bytes=o.bytes_out, quality=o.quality,
                crop=o.crop, output_path=path)


# --------------------------------------------------------------------------- lists and summary
def thumb_grid(items: list[dict], ncols: int, adjust_state_key: str | None = None):
    """Rows of equal-height thumbnails with fixed two-line captions so rows stay aligned.
    items: dict(img, title, label, id, warn, manual, seen)."""
    for r in range(0, len(items), ncols):
        cols = st.columns(ncols)
        for col, it in zip(cols, items[r:r + ncols]):
            with col:
                st.image(it["img"], width="stretch")
                cls = "warn" if it.get("warn") else ("manual" if it.get("manual") else "")
                seen = it.get("seen")
                mark = (f'<span class="mpe-seen" style="width:1rem;height:1rem;font-size:.7rem;margin-right:.3rem" '
                        f'title="{html.escape(history.describe(seen), quote=True)}">!</span>') if seen else ""
                st.markdown(f'<div class="thumbcap"><div class="t" title="{html.escape(it["title"])}">{mark}'
                            f'{html.escape(it["title"])}</div><div class="l {cls}">{html.escape(it["label"])}'
                            f'</div></div>', unsafe_allow_html=True)
                if adjust_state_key:
                    st.button("Adjust", key=f"{adjust_state_key}-{it['id']}", type="tertiary",
                              on_click=st.session_state.__setitem__, args=(adjust_state_key, it["id"]))


def metrics(n_sources: int, outputs: list[Output], src_bytes: int):
    made = [o for o in outputs if o.status in ("ok", "over cap")]
    with st.container(border=True):
        c = st.columns(4)
        c[0].metric("Images", n_sources)
        c[1].metric("Files created", len(made))
        c[2].metric("Source total", human(src_bytes))
        c[3].metric("Output total", human(sum(o.bytes_out for o in made)))


def resolve_duplicates(sources: list[Source], s: Settings, key: str):
    conflicts = core.find_duplicates(sources, s)
    exact = [x for x in sources if x.duplicate_of]
    if exact:
        st.info(f"Skipping {len(exact)} exact duplicate(s) with the same name, dimensions and file size: "
                + ", ".join(x.filename for x in exact[:8]) + (" ..." if len(exact) > 8 else ""))
    if conflicts:
        with st.container(border=True):
            st.markdown("**Same name, different files.** Choose which to keep.")
            for c in conflicts:
                opts = [x.id for x in c] + ["all"]
                labels = {x.id: f"Keep {x.label}" for x in c}
                labels["all"] = "Keep all (numbered -2, -3 ...)"
                choice = st.radio(f"`{c[0].base}`", opts, format_func=labels.get, key=f"{key}-dup-{c[0].base}")
                core.apply_choice(c, choice)
    skipped = [x for x in sources if x.duplicate_of]
    rows = [Output(x.filename, "-", "", None, x.dims, None, x.nbytes, status="skipped",
                   note=f"duplicate of {x.duplicate_of}" if not x.duplicate_of.startswith("name clash")
                   else x.duplicate_of) for x in skipped]
    return [x for x in sources if not x.duplicate_of], rows


def format_picker():
    """Output format, shown on the page right under where photos come in. Applies immediately."""
    labels = list(core.FORMAT_LABELS.values())
    keys = {v: k for k, v in core.FORMAT_LABELS.items()}
    cur = core.FORMAT_LABELS[st.session_state.get("fmt_value", "webp")]

    def changed():
        v = st.session_state.get("fmt_pick") or cur          # clicking the selected one keeps it
        st.session_state["fmt_value"] = keys[v]

    st.session_state["fmt_pick"] = cur
    c1, c2 = st.columns([2, 3], vertical_alignment="center")
    with c1:
        st.segmented_control("Output format", labels, key="fmt_pick", on_change=changed, width="stretch")
    fmt = st.session_state.get("fmt_value", "webp")
    c2.caption({"webp": "WebP: smallest files, best for websites.",
                "jpeg": "JPEG: for email, social media, print and older systems.",
                "png": "PNG: lossless and much larger for photos. Best for graphics.",
                "gif": "GIF: 256 colours only, photos will look grainy."}[fmt])


def release():
    core.release_memory()


def np_mean_color(img: Image.Image, x: int, y: int) -> str:
    patch = np.asarray(img)[max(0, y - 2):y + 3, max(0, x - 2):x + 3].reshape(-1, 3).mean(0)
    return "#%02X%02X%02X" % tuple(int(round(v)) for v in patch)
