"""Upload page: quick jobs with the crop editor and a large before / after preview."""
from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime
from pathlib import Path

import streamlit as st

from .. import core, history, native
from ..core import EYE_Y, Source
from ..paths import INPUT_DIR, OUTPUT_DIR
from .common import (adj_key, progress_note, adjustments, before_image, cached_crop, crop_editor, current_settings,
                     fill_pads, fingerprint, format_picker, has_effect, hist_row, human, metrics,
                     preview_html, release, resolve_duplicates, seen_badge, thumb_grid)

IMAGE_TYPES = sorted(e[1:] for e in core.INPUT_EXTS)


def _image_paths(folder: str) -> list[str]:
    try:
        return sorted(str(p) for p in Path(folder).iterdir()
                      if p.is_file() and p.suffix.lower() in core.INPUT_EXTS and not p.name.startswith("."))
    except OSError:
        return []


def _browse():
    """One picker for photos and folders (a folder adds every photo directly inside it)."""
    picked = native.browse("Choose photos, or a folder of photos", str(INPUT_DIR))
    if not picked:
        return
    have = st.session_state.get("up_paths", [])
    new, folders = [], 0
    for p in picked:
        if os.path.isdir(p):
            folders += 1
            new += _image_paths(p)
        elif Path(p).suffix.lower() in core.INPUT_EXTS:
            new.append(p)
    added = [p for p in dict.fromkeys(new) if p not in have]
    st.session_state["up_paths"] = have + added
    st.session_state["up_msg"] = (f"Added {len(added)} photo(s)" + (f" from {folders} folder(s)." if folders else ".")
                                  if added else "No new photos found in what you chose.")


def _clear_paths():
    st.session_state.pop("up_paths", None)


def _record(action, src, fp, o, s, path=None):
    history.record([hist_row(action, src, fp, o, s, path)])


def _save(src, fp, o, s):
    folder = OUTPUT_DIR / o.folder
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / o.filename
    path.write_bytes(o.data)
    _record("save", src, fp, o, s, str(path))
    st.session_state["up_saved"] = str(path)


def collect():
    """Photos on this page: uploaded files plus photos opened from a folder on disk.
    Returns {id: (Source, src, mkey)} where src is bytes (upload) or a path."""
    items = {}
    for f in st.session_state.get("up_files") or []:
        fid = getattr(f, "file_id", None) or f"{f.name}-{f.size}"
        data = f.getvalue()
        items[fid] = (Source(fid, f.name, f.size, _probe(data)), data, "upload")
    for p in st.session_state.get("up_paths", []):
        try:
            stt = os.stat(p)
        except OSError:
            continue
        items[p] = (Source(p, os.path.basename(p), stt.st_size, _probe(p)), p, stt.st_mtime)
    return items


def _probe(src):
    try:
        return core.probe(src)
    except Exception:
        return None


@st.fragment
def editor_area(sel: str, item, base: str, seen_row):
    """Crop editor (left) and result (right) for one photo. Runs as a fragment so moving the crop
    only refreshes this area."""
    s = current_settings()
    src_obj, src, mkey = item
    store = adjustments()
    key = adj_key(sel, src_obj.dims, s.crop.ratio)
    box = store.get(key)
    o = cached_crop(sel, mkey, src, src_obj.filename, s, base, tuple(box) if box else None)
    if o.status == "error":
        st.error(f"{src_obj.filename}: {o.note}")
        return
    auto = o.crop_box if box is None else cached_crop(sel, mkey, src, src_obj.filename, s, base, None).crop_box
    pad = fill_pads(o.src_size, s)
    left, right = st.columns(2, gap="large")
    with left:
        guide = EYE_Y if (s.crop.slug == "profile" and s.anchor == "face") else None
        height = crop_editor(key, sel, mkey, src, o.src_size, auto, s.crop.ratio, pad,
                             s.bg_color if s.white_bg else None, guide)
    with right:
        view = st.session_state.get(f"cmp-{sel}") or "After" if has_effect(s) else "After"
        if view == "Before":
            st.markdown(preview_html(before_image(sel, mkey, src, s, tuple(o.crop_box)), "image/jpeg", height),
                        unsafe_allow_html=True)
        else:
            st.markdown(preview_html(o.data, o.mime, height), unsafe_allow_html=True)
        if has_effect(s):
            st.segmented_control("Compare", ["After", "Before"], default="After", key=f"cmp-{sel}",
                                 label_visibility="collapsed")
        st.markdown(f'<div class="mpe-info"><b>{o.folder}/{o.filename}</b><br>'
                    f'{o.src_size[0]}x{o.src_size[1]} to <b>{o.out_size[0]}x{o.out_size[1]}</b> · '
                    f'{human(o.bytes_in)} to <b>{human(o.bytes_out)}</b>'
                    + (f" · quality {o.quality}" if o.quality else "")
                    + (f"<br>{o.note}" if o.note else "") + "</div>", unsafe_allow_html=True)
        fp = fingerprint(sel, mkey, src)
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.download_button("Download", o.data, o.filename, o.mime, key=f"dl-{sel}", type="primary",
                               on_click=_record, args=("download", src_obj, fp, o, s))
            st.button("Save to Output folder", key=f"save-{sel}", on_click=_save, args=(src_obj, fp, o, s))
            seen_badge(seen_row)
        saved = st.session_state.pop("up_saved", None)
        if saved:
            c1, c2 = st.columns([3, 1], vertical_alignment="center")
            c1.caption(f"Saved to `{saved}`")
            if native.IS_MAC and c2.button("Show in Finder", type="tertiary", key=f"reveal-{sel}"):
                native.reveal(saved)


def upload_page():
    s = current_settings()
    native_pick = native.can_pick()
    st.file_uploader("Drop photos here" if native_pick else "Drop photos here, or click to browse",
                     type=IMAGE_TYPES, accept_multiple_files=True, key="up_files")
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        if native_pick:
            st.button("Browse...", type="primary", on_click=_browse, key="up-browse")
        if st.session_state.get("up_paths"):
            n = len(st.session_state["up_paths"])
            st.button(f"Clear {n} browsed photo{'s' if n != 1 else ''}", type="tertiary", on_click=_clear_paths,
                      key="up-clear")
        msg = st.session_state.pop("up_msg", None)
        if msg:
            st.caption(msg)
    format_picker()

    items = collect()
    if not items:
        st.info("Add photos to start: drop them above"
                + (", or use Browse to choose photos or whole folders anywhere on this Mac." if native_pick else ".")
                + " For big batches of full-resolution photos, use Batch.")
        return
    if len(items) > 40:
        st.caption("Tip: for batches this size, Batch is faster and writes straight to a folder.")

    sources = [it[0] for it in items.values()]
    keep, skipped = resolve_duplicates(sources, s, "up")
    unreadable = [x for x in keep if not x.dims]
    for x in unreadable:
        st.error(f"{x.filename}: not a supported image, or the file is damaged.")
    keep = [x for x in keep if x.dims]
    if not keep:
        return

    store = adjustments()
    outputs, by_src = [], {}
    prog = st.progress(0.0, text=progress_note(s, "Converting..."))
    for i, src in enumerate(keep, 1):
        _, data, mkey = items[src.id]
        box = store.get(adj_key(src.id, src.dims, s.crop.ratio))
        by_src[src.id] = cached_crop(src.id, mkey, data, src.filename, s, src.base, tuple(box) if box else None)
        outputs.append(by_src[src.id])
        prog.progress(i / len(keep), text=progress_note(s, f"Converting {i}/{len(keep)}"))
    prog.empty()
    release()

    fps = {x.id: fingerprint(x.id, items[x.id][2], items[x.id][1]) for x in keep}
    seen = history.recent([f for f in fps.values() if f], s.spec, st.session_state.get("_sid"))

    st.subheader("Preview and adjust")
    names = {x.id: x.filename for x in keep}
    if st.session_state.get("up-sel") not in names:
        st.session_state.pop("up-sel", None)
    sel = st.selectbox("Photo", list(names), format_func=names.get, label_visibility="collapsed", key="up-sel")
    editor_area(sel, items[sel], next(x.base for x in keep if x.id == sel), seen.get(fps[sel]))

    st.divider()
    st.subheader("All crops")
    st.caption("Click Adjust under a photo to edit it above. An exclamation mark means the photo was already "
               f"processed with this format and size in the last {history.RECENT_DAYS} days.")
    grid = [dict(img=o.data, title=o.filename, id=x.id, manual=o.crop == "manual",
                 warn="headroom" in o.crop or "check hair" in o.crop, seen=seen.get(fps[x.id]),
                 label=f"{o.out_size[0]}x{o.out_size[1]} · {human(o.bytes_out)} · {o.crop}")
            for x in keep if (o := by_src[x.id]).data]
    thumb_grid(grid, 6, adjust_state_key="up-sel")
    for o in outputs:
        if o.status in ("error", "over cap"):
            (st.error if o.status == "error" else st.warning)(f"{o.source}: {o.note}")

    st.divider()
    metrics(len(sources), outputs, sum(x.nbytes for x in sources))
    made = [o for o in outputs if o.status in ("ok", "over cap")]
    b1, b2 = st.columns(2)
    if made:
        sig = hash(tuple((o.folder, o.filename, o.bytes_out) for o in made))
        ready = st.session_state.get("zip_ready")
        if ready and ready[0] == sig:
            rows = [hist_row("zip", x, fps[x.id], by_src[x.id], s) for x in keep if by_src[x.id].data]
            b1.download_button("Download all (.zip)", ready[1], "mosaic-photo-editor.zip", "application/zip",
                               type="primary", width="stretch", on_click=history.record, args=(rows,))
        elif b1.button(f"Prepare zip ({len(made)} files)", type="primary", width="stretch"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
                for o in made:
                    z.writestr(f"{o.folder}/{o.filename}", o.data)
            st.session_state["zip_ready"] = (sig, buf.getvalue())
            st.rerun()
    b2.download_button("Download CSV report", core.report_csv(outputs + skipped),
                       f"mosaic-report-{datetime.now():%Y%m%d-%H%M}.csv", "text/csv", width="stretch",
                       on_click="ignore")
