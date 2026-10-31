"""Settings panel (left sidebar).

Runs as a fragment: changing a setting only refreshes the panel, never the photos. Nothing is
reprocessed until Apply changes is pressed, so several settings can be changed without waiting.
Sections, top to bottom: Output, Cropping, Head and face, Image background.
"""
from __future__ import annotations

import re

import streamlit as st
from PIL import Image

from .. import ai, core, history, native, timing
from ..core import CROP_PRESETS, NO_CROP, CropPreset, Settings
from ..paths import INPUT_DIR, OUTPUT_DIR, count_files, empty_folder
from .common import FORMAT_HELP, hint, np_mean_color, section, subhead
from .components import danger_button

try:
    from streamlit_image_coordinates import streamlit_image_coordinates
    EYEDROPPER = True
except ImportError:
    EYEDROPPER = False

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@st.cache_resource(show_spinner=False)
def ai_ok() -> bool:
    return ai.available()


# --------------------------------------------------------------------------- background colour
def _norm_hex(v: str) -> str | None:
    v = v.strip()
    if not HEX_RE.match(v):
        return None
    v = v.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return "#" + v.upper()


def _hex_typed():
    v = _norm_hex(st.session_state.get("bg_hex", ""))
    if v:
        st.session_state.update(bg_hex=v, bg_pick=v, bg_hex_bad=False)
    else:
        st.session_state.update(bg_hex=st.session_state.get("bg_pick", "#FFFFFF"), bg_hex_bad=True)


def _pick_changed():
    st.session_state.update(bg_hex=st.session_state["bg_pick"].upper(), bg_hex_bad=False)


def _set_color(v: str):
    st.session_state.update(bg_hex=v, bg_pick=v, bg_hex_bad=False, eyedrop_open=False)


def _wb_changed():
    if st.session_state.get("wb_toggle"):
        st.session_state["wb_pending"] = True          # ask before switching on
    else:
        st.session_state["wb_confirmed"] = False


def _wb_yes():
    st.session_state.update(wb_confirmed=True, wb_pending=False)


def _wb_no():
    st.session_state.update(wb_toggle=False, wb_confirmed=False, wb_pending=False)


def _wb_dialog_body():
    if not st.session_state.get("wb_pending"):        # answered: close the dialog
        st.rerun()
    st.markdown("Mosaic Photo-Editor will use AI to replace the background of **profile crops** with a plain "
                "colour (white unless you choose another). It runs on this computer. Hair edges may need a "
                "quick check in the preview.")
    st.markdown("**Do you want to continue?**")
    c1, c2 = st.columns(2)
    c1.button("Yes", type="primary", width="stretch", on_click=_wb_yes, key="wb-yes")
    c2.button("No", width="stretch", on_click=_wb_no, key="wb-no")


wb_dialog = st.dialog("Replace background", on_dismiss=_wb_no)(_wb_dialog_body)


def _eyedrop_body():
    if not st.session_state.get("eyedrop_open"):
        st.rerun()
    st.caption("Click anywhere on the image to pick its colour.")
    up = st.file_uploader("Pick from another image, e.g. a graphic template",
                          type=["png", "jpg", "jpeg", "webp"], key="eyedrop_up")
    src = Image.open(up).convert("RGB") if up else st.session_state.get("eyedrop_img")
    if src is None:
        st.info("Upload an image above, or open a photo in the editor first.")
    else:
        disp = src.copy()
        disp.thumbnail((640, 480), Image.LANCZOS)
        pos = streamlit_image_coordinates(disp, width=disp.width, cursor="crosshair",
                                          key=f"eyedrop-{getattr(up, 'file_id', 'cur')}")
        if pos:
            x = min(max(int(pos["x"]), 0), disp.width - 1)
            y = min(max(int(pos["y"]), 0), disp.height - 1)
            st.session_state["eyedrop_val"] = np_mean_color(disp, x, y)
    val = st.session_state.get("eyedrop_val")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f'<div style="height:38px;border-radius:6px;border:1px solid #ccc;background:{val or "#FFFFFF"}">'
                f'</div>', unsafe_allow_html=True)
    c2.button(f"Use {val}" if val else "Use colour", type="primary", disabled=not val, width="stretch",
              on_click=_set_color, args=(val,), key="eyedrop-use")
    c3.button("Cancel", width="stretch", key="eyedrop-cancel",
              on_click=st.session_state.update, kwargs=dict(eyedrop_open=False))


eyedrop_dialog = st.dialog("Eyedropper", width="large")(_eyedrop_body)


def _ai_missing():
    st.caption(":orange[" + ai.INSTALL_HINT + "]")


def background_controls(portrait: bool):
    """Returns (replace_bg, extend_top, bg_color, ai_extend, ext_top, ext_side, ext_bottom)."""
    white_bg, extend_top, bg_color = False, False, "#FFFFFF"
    if portrait:
        st.toggle("Replace background (AI)", key="wb_toggle", on_change=_wb_changed, disabled=not ai_ok())
        if st.session_state.get("wb_toggle") and st.session_state.get("wb_confirmed") and ai_ok():
            st.session_state.setdefault("bg_pick", "#FFFFFF")
            st.session_state.setdefault("bg_hex", "#FFFFFF")
            c1, c2 = st.columns([1, 3], vertical_alignment="bottom")
            c1.color_picker("Colour", key="bg_pick", on_change=_pick_changed, label_visibility="collapsed")
            c2.text_input("Background colour (hex)", key="bg_hex", on_change=_hex_typed, max_chars=7,
                          label_visibility="collapsed", placeholder="#FFFFFF")
            b1, b2 = st.columns(2)
            b1.button("Eyedropper", disabled=not EYEDROPPER, width="stretch",
                      on_click=st.session_state.update, kwargs=dict(eyedrop_open=True, eyedrop_val=None))
            b2.button("White", on_click=_set_color, args=("#FFFFFF",), width="stretch")
            if st.session_state.get("bg_hex_bad"):
                st.caption(":orange[Not a valid hex code. Use 6 characters, e.g. #F2F2F2.]")
            extend_top = st.checkbox("Add headroom to tight photos", True, key="wb_extend")
            st.caption(f"Adds background colour above photos with too little space over the head "
                       f"(up to {int(core.EXTEND_MAX * 100)}% of the photo height).")
            white_bg, bg_color = True, st.session_state["bg_pick"].upper()

    if portrait:
        st.divider()
    on = st.toggle("AI extender", key="ext_toggle", disabled=not ai_ok())
    if not ai_ok():
        _ai_missing()
    if not (on and ai_ok()):
        return white_bg, extend_top, bg_color, False, 0.10, 0.0, 0.0
    kind = "portrait" if portrait else "other"
    top = st.slider("Max extra above (% of photo height)", 0, 50, 10, step=5, key=f"ext_top_{kind}") / 100
    side = st.slider("Max extra each side (% of photo width)", 0, 15 if portrait else 50, 0,
                     key=f"ext_side_{kind}") / 100
    bottom = st.slider("Max extra below (% of photo height)", 0, 10 if portrait else 50, 0,
                       step=1 if portrait else 5, key=f"ext_bottom_{kind}") / 100
    if portrait:
        st.caption("Above and beside, matching backdrop is generated. Below, the clothing is lengthened "
                   "without inventing anything: plain rows between buttons, seams and folds are repeated.")
    else:
        st.caption("Generated areas match the photo's backdrop. Leave 'below' at 0 for photos of people.")
    if white_bg:
        st.caption("Background is being replaced, so extensions use the background colour.")
    return white_bg, extend_top, bg_color, True, top, side, bottom


# --------------------------------------------------------------------------- panel
def _apply(draft: Settings):
    st.session_state["applied"] = draft
    st.session_state["_applied_now"] = True
    st.session_state.pop("zip_ready", None)


def _clear_caches():
    """Drop every in-memory image cache and unload the AI models. Files on disk and crop
    adjustments are kept."""
    st.cache_data.clear()
    st.cache_resource.clear()
    ai.clear_caches()
    ai.unload()
    for k in ("zip_ready", "folder_result", "eyedrop_img"):
        st.session_state.pop(k, None)
    core.release_memory()
    st.session_state["_cache_cleared"] = True


@st.cache_data(ttl=20, show_spinner=False)
def folder_counts() -> tuple[int, int]:
    """Files in Input and Output (cached briefly: Output can hold thousands of files)."""
    return count_files(INPUT_DIR), count_files(OUTPUT_DIR)


def _empty_folders():
    removed, errors = 0, []
    for folder in (INPUT_DIR, OUTPUT_DIR):
        n, e = empty_folder(folder)
        removed += n
        errors += e
    st.session_state["_emptied"] = (removed, errors)
    folder_counts.clear()
    st.session_state.pop("folder_result", None)
    st.session_state.pop("up_paths", None)


def _format_changed():
    st.session_state["fmt_value"] = st.session_state.get("fmt_pick") or "webp"
    st.session_state["_fmt_changed"] = True
    st.session_state.pop("zip_ready", None)


def _convert():
    st.session_state["_convert_now"] = True


def _quit():
    st.session_state["_quitting"] = True
    native.quit_app()


@st.fragment
def settings_panel(page: str = ""):
    if st.session_state.get("wb_pending"):
        wb_dialog()
    elif st.session_state.get("eyedrop_open"):
        eyedrop_dialog()
    fmt = st.session_state.get("fmt_value", "webp")

    # ---- Output: format, file names, quality (and Convert on the Batch page)
    output_box = st.container(border=True)
    with output_box:
        section("Output")
        st.session_state["fmt_pick"] = fmt
        st.selectbox("Format", list(core.FORMAT_LABELS), key="fmt_pick", format_func=core.FORMAT_LABELS.get,
                     on_change=_format_changed)
        st.caption(FORMAT_HELP[fmt])
        if st.session_state.pop("_fmt_changed", False):
            st.rerun(scope="app")                      # the format applies straight away
        subhead("File names")
        clean = st.toggle("Clean names", True, key="o_clean")
        preset_slug = st.session_state.get("o_shape_slug", "profile")
        add_variant = st.checkbox(f'Add "-{preset_slug}" to file names', True, key="o_variant")
        add_dims = st.checkbox("Add dimensions", False, key="o_dims")
        prefix = st.text_input("Prefix (optional)", "", placeholder="e.g. faculty", key="o_prefix")
        subhead("Quality")
        lossless = st.toggle("Lossless WebP", False, key="o_lossless", disabled=fmt != "webp")
        lossy = fmt == "jpeg" or (fmt == "webp" and not lossless)
        quality = st.slider("Quality", 1, 100, 82, disabled=not lossy, key="o_quality")
        sharpen = st.toggle("Light sharpening after resizing", True, key="o_sharpen")
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            keep_meta = st.toggle("Keep EXIF metadata", False, key="o_meta", width="content")
            hint("Off removes camera and GPS data.")

    # ---- Cropping: shape and size
    with st.container(border=True):
        section("Cropping")
        names = [p.name for p in CROP_PRESETS] + ["Custom"]
        pick = st.selectbox("Shape", names, key="o_shape")
        no_crop = pick == NO_CROP.name
        if no_crop:
            longest = st.number_input("Longest side (px)", 16, 10000, NO_CROP.max_size[0], step=100, key="o_longest")
            crop = CropPreset(NO_CROP.name, NO_CROP.slug, None, (int(longest), int(longest)), NO_CROP.cap_kb)
        elif pick == "Custom":
            c1, c2 = st.columns(2)
            rw = c1.number_input("Ratio width", 1, 100, 4, key="o_rw")
            rh = c2.number_input("Ratio height", 1, 100, 5, key="o_rh")
            mw = c1.number_input("Max width (px)", 16, 10000, 1200, step=50, key="o_mw")
            mh = c2.number_input("Max height (px)", 16, 10000, 1500, step=50, key="o_mh")
            slug = core.slugify(st.text_input("Label", "custom", key="o_label")) or "custom"
            crop = CropPreset("Custom", slug, (int(rw), int(rh)), (int(mw), int(mh)), 200)
        else:
            crop = CROP_PRESETS[names.index(pick)]
        st.session_state["o_shape_slug"] = crop.slug
        r, m = crop.ratio, crop.max_size
        if no_crop:
            st.caption(f"No cropping: the whole photo is kept. Photos longer than {m[0]} px on either side are "
                       "reduced; smaller photos keep their resolution.")
            anchor = "center"
        else:
            st.caption(f"Crops to {r[0]}:{r[1]}. Images larger than {m[0]}x{m[1]} are reduced to it; smaller "
                       "images keep their resolution.")
            anchors = {"Face detection": "face", "Top-weighted": "top", "Centre": "center"}
            if not core.FACE_DETECTION_AVAILABLE:
                anchors.pop("Face detection")
            anchor = anchors[st.radio("Crop position", list(anchors), horizontal=True, key="o_anchor")]
        subhead("File size cap")
        crop_cap = st.number_input("Size cap (KB)", 0, 20000, crop.cap_kb, step=10, key=f"cap-{crop.slug}",
                                   disabled=not lossy, label_visibility="collapsed")
        st.caption("0 = no cap. Quality is lowered step by step until the file fits "
                   f"(never below {core.MIN_QUALITY}). Not for PNG, GIF or lossless WebP.")
        with st.expander("Recommended size caps"):
            st.markdown("| Use | Cap |\n|---|---|\n" +
                        "\n".join(f"| {u} | {c} |" for u, c in core.RECOMMENDED_CAPS))

    # ---- Head and face
    head_frac = 0.0
    with st.container(border=True):
        section("Head and face")
        if no_crop:
            st.caption("Not used when Shape is None (no cropping).")
        elif anchor == "face":
            if st.checkbox("Consistent head size", crop.slug == "profile", key=f"head-{crop.slug}"):
                head_frac = st.slider("Face height (% of frame height)", 25, 55, int(core.HEAD_FRAC * 100),
                                      key="head-pct") / 100
                st.caption(f"Zooms in on wide shots so every face fills the same share of the frame. "
                           f"{int(core.HEAD_FRAC * 100)}% matches a typical professional headshot; higher "
                           "is tighter. Eyes sit about a third of the way down.")
            else:
                st.caption("Faces are centred but keep the size they have in the photo.")
        else:
            st.caption("Choose Face detection under Cropping to size and place heads consistently.")

    # ---- Image background
    with st.container(border=True):
        section("Image background")
        if ai_ok() and ai.warm_state() == "loading":
            st.caption(":orange[Preparing the AI models in the background. The first AI result after "
                       "starting the app can take a little longer.]")
        portrait = crop.slug == "profile" and pick != "Custom"
        if no_crop:
            st.caption("Not used when Shape is None (no cropping).")
            white_bg, extend_top, bg_color, ai_extend, ext_top, ext_side, ext_bottom = (
                False, False, "#FFFFFF", False, 0.10, 0.0, 0.0)
        else:
            white_bg, extend_top, bg_color, ai_extend, ext_top, ext_side, ext_bottom = background_controls(portrait)

    if not lossy:              # PNG, GIF, lossless WebP: no quality to lower, so no cap
        crop_cap = 0
    draft = Settings(crop=crop, anchor=anchor, head_frac=head_frac, crop_cap_kb=int(crop_cap), quality=quality,
                     lossless=lossless, sharpen=sharpen, keep_meta=keep_meta, clean_names=clean,
                     add_variant=add_variant, add_dims=add_dims, prefix=prefix, fmt="webp",
                     white_bg=white_bg, extend_top=extend_top, bg_color=bg_color, ai_extend=ai_extend,
                     ext_top=ext_top, ext_side=ext_side, ext_bottom=ext_bottom)
    if "applied" not in st.session_state:
        st.session_state["applied"] = draft
    pending = draft != st.session_state["applied"]

    if page == "batch":                                # Convert sits at the bottom of the Output box
        with output_box:
            st.button("Convert", type="primary", width="stretch", key="convert-btn", on_click=_convert,
                      help="Convert the photos in the Origin Folder (applies any changed settings first).")
    if st.session_state.pop("_convert_now", False):
        st.session_state["applied"] = draft
        st.session_state["batch_go"] = True
        st.session_state.pop("zip_ready", None)
        st.rerun(scope="app")

    with st.container(key="apply-bar"):
        st.button("Apply changes" if pending else "Settings applied", type="primary", width="stretch",
                  disabled=not pending, key="apply-btn", on_click=_apply, args=(draft,))
        if st.session_state.pop("_applied_now", False):
            st.rerun(scope="app")                      # reprocess the photos with the new settings
        if pending:
            st.caption("You have changes that aren't applied yet.")

    st.divider()
    st.button("Clear cached images", width="stretch", on_click=_clear_caches, key="clear-cache")
    st.caption("Cached previews and AI results cleared; AI models unloaded."
               if st.session_state.pop("_cache_cleared", False) else
               "Frees memory. Your photos, converted files and crop adjustments are kept.")

    n_in, n_out = folder_counts()
    emptied = st.session_state.pop("_emptied", None)
    danger_button("empty-folders", "Hold to empty Input and Output", _empty_folders,
                  help=(f"Moves everything in the Input ({n_in} files) and Output ({n_out} files) folders to "
                        "the Trash."),
                  done="Emptied", disabled=not (n_in or n_out))
    if emptied:
        n, errors = emptied
        (st.warning if errors else st.success)(f"Moved {n} item(s) to the Trash."
                                               + (f" {len(errors)} could not be moved: " + "; ".join(errors[:3])
                                                  if errors else ""))

    with st.expander("History"):
        n, first = history.stats()
        st.caption(f"{n} processed file(s) recorded. Photos processed with the same format and size in the "
                   f"last {history.RECENT_DAYS} days are marked with an exclamation mark.")
        if st.button("Clear history", key="clear-history", disabled=not n):
            history.clear()
            st.rerun(scope="fragment")

    with st.expander("Timing log"):
        rows = timing.summary()
        st.caption("How long each step took in the last 30 days, to compare this Mac with Docker. "
                   "Results reused from the cache are not timed.")
        if rows:
            st.markdown('<div class="mpe-info">' + "<br>".join(
                f"<b>{r['step']}</b> · {r['runtime']}: {r['median s']:.2f} s typical ({r['times']}x)" for r in rows)
                + "</div>", unsafe_allow_html=True)
        else:
            st.caption("Nothing timed yet.")
        c1, c2 = st.columns(2)
        c1.download_button("CSV", timing.csv_bytes(), "mosaic-timings.csv", "text/csv",
                           width="stretch", on_click="ignore", key="dl-timings")
        if c2.button("Clear", width="stretch", key="clear-timings", disabled=not rows):
            timing.clear()
            st.rerun(scope="fragment")
        if native.IS_MAC and st.button("Show in Finder", type="tertiary", key="reveal-timings",
                                       disabled=not timing.LOG_FILE.exists()):
            native.reveal(str(timing.LOG_FILE))

    if native.in_window() or not native.IN_DOCKER:
        st.button("Quit Mosaic Photo-Editor", width="stretch", type="tertiary", on_click=_quit, key="quit")
