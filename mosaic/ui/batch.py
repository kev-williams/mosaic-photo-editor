"""Batch page: large batches read straight from a folder and written to another folder.
Originals are only read, never changed or moved."""
from __future__ import annotations

import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import streamlit as st

from .. import core, history, native, timing
from ..core import EYE_Y, Output, Settings, Source
from ..paths import IN_DOCKER, INPUT_DIR, OUTPUT_DIR, RECENT_FILE, RECENT_OUT_FILE, read_json, write_json
from .common import (adj_key, progress_note, adjustments, crop_editor, current_settings, extend_fn_for, fill_pads,
                     fingerprint, friendly_label, hist_row, human, mask_fn_for, metrics, preview_html, release,
                     resolve_duplicates, session_id, thumb_grid, work_image)

WORKERS = int(os.environ.get("WORKERS", min(4, os.cpu_count() or 2)))
SLOTS = threading.BoundedSemaphore(int(os.environ.get("FULL_SIZE_WORKERS", 2)))   # caps peak memory
CHOOSE = "Choose Folder..."
DEFAULT_OUT = "__default__"
RECENT_MAX = 4
PER_PAGE = 30
FILTERS = ["All", "Needs attention", "No face found", "Adjusted", "Not adjusted", "Processed before", "New"]
SORTS = ["Name (A-Z)", "Name (Z-A)", "Newest first", "Oldest first", "Largest first", "Needs attention first"]


def explain_read_error(e: Exception) -> str:
    msg = f"{type(e).__name__}: {e}"
    errno = getattr(e, "errno", None)
    if isinstance(e, PermissionError) or errno in (1, 13):
        return ("macOS blocked access (privacy permission). Allow it in System Settings > Privacy & Security > "
                f"Files and Folders, then try again. [{msg}]")
    if errno in (11, 35) or "deadlock" in str(e).lower() or "timed out" in str(e).lower():
        return ("file is in iCloud/OneDrive but not downloaded to this Mac. In Finder, right-click the folder > "
                f"Download Now. [{msg}]")
    if type(e).__name__ == "UnidentifiedImageError":
        return f"not a supported image, or the file is damaged. [{msg}]"
    return msg


@st.cache_data(max_entries=5000, show_spinner=False)
def probe_info(path: str, mtime: float):
    """(dimensions, error) - reads the header and the first bytes so access problems show up here."""
    try:
        with open(path, "rb") as fh:
            fh.read(1024)
        return core.probe(path), ""
    except Exception as e:
        return None, explain_read_error(e)


def load_recent() -> list[str]:
    return read_json(RECENT_FILE, [])[:8]


def save_recent(path: str):
    write_json(RECENT_FILE, ([path] + [p for p in load_recent() if p != path])[:8])


def load_recent_out() -> list[str]:
    return read_json(RECENT_OUT_FILE, [])[:8]


def save_recent_out(path: str):
    write_json(RECENT_OUT_FILE, ([path] + [p for p in load_recent_out() if p != path])[:8])


def short_path(p: str) -> str:
    home = str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


def folder_label(p: str) -> str:
    """'Shoot  ·  ~/Pictures/Mosaic Photo-Editor/Input': folder name first, so long paths stay readable."""
    if p == CHOOSE:
        return p
    q = Path(p)
    return f"{q.name or p}  ·  {short_path(str(q.parent))}" if q.name else p


def normalize_path(raw: str) -> str:
    """Accept paths as copied from Finder, dragged into Terminal (escaped spaces) or quoted."""
    p = raw.strip().strip("'\"").replace("\\ ", " ")
    if p.startswith("file://"):
        p = unquote(p[7:])
    return os.path.expanduser(p).rstrip("/") or "/"


def _set_input(path: str):
    path = normalize_path(path)
    st.session_state["in-path"] = path
    st.session_state.pop("out-path", None)          # destination follows the origin folder's name again
    st.session_state.pop("fd-page", None)
    if os.path.isdir(path):
        save_recent(path)


def _origin_changed():
    v = st.session_state.get("in-pick")
    if v != CHOOSE:
        _set_input(v)
    elif native.can_pick():
        p = native.pick_folder("Choose the folder with your photos",
                               st.session_state.get("in-path") or str(INPUT_DIR))
        if p:
            _set_input(p)
    else:                                            # Docker / plain browser: type the path instead
        st.session_state["in-typing"] = True


def _origin_typed():
    v = st.session_state.get("in-typed", "").strip()
    if v:
        _set_input(v)
    st.session_state["in-typing"] = False


def _dest_changed():
    v = st.session_state.get("out-pick")
    if v == DEFAULT_OUT:
        st.session_state.pop("out-path", None)
    elif v != CHOOSE:
        st.session_state["out-path"] = v
        save_recent_out(v)
    elif native.can_pick():
        p = native.pick_folder("Choose where converted files go", str(OUTPUT_DIR))
        if p:
            st.session_state["out-path"] = p
            save_recent_out(p)
    else:
        st.session_state["out-typing"] = True


def _dest_typed():
    v = st.session_state.get("out-typed", "").strip()
    if v:
        p = normalize_path(v)
        p = p if os.path.isabs(p) else str(OUTPUT_DIR / p)
        st.session_state["out-path"] = p
        save_recent_out(p)
    st.session_state["out-typing"] = False


def folder_pickers() -> tuple[Path | None, Path, bool, bool]:
    """Origin and destination folders: each one dropdown with recent folders and Choose Folder... at the end.
    Returns (origin, destination, include subfolders, save report)."""
    recent = [p for p in load_recent() if os.path.isdir(p)]
    if not st.session_state.get("in-path"):
        st.session_state["in-path"] = recent[0] if recent else str(INPUT_DIR)
    cur = st.session_state["in-path"]
    in_dir = Path(cur)
    out_default = str(OUTPUT_DIR / (in_dir.name if in_dir != INPUT_DIR else "Batch"))
    cur_out = st.session_state.get("out-path") or DEFAULT_OUT
    recent_out = [p for p in load_recent_out() if p != out_default]

    left, right = st.columns(2, gap="medium")
    with left.container(border=True):
        opts = list(dict.fromkeys([cur] + recent))[:RECENT_MAX] + [CHOOSE]
        st.session_state["in-pick"] = cur
        st.selectbox("Origin Folder", opts, key="in-pick", on_change=_origin_changed,
                     format_func=folder_label)
        if st.session_state.get("in-typing"):
            st.text_input("Folder path", key="in-typed", on_change=_origin_typed, placeholder="/data/Input/Shoot",
                          help="Paths inside the folders shared with Docker (see docker/env.example).")
        recursive = st.checkbox("Include subfolders", False, key="fd-recursive")
    with right.container(border=True):
        extra = [] if cur_out == DEFAULT_OUT else [cur_out]
        opts = [DEFAULT_OUT] + list(dict.fromkeys(extra + recent_out))[:RECENT_MAX] + [CHOOSE]
        st.session_state["out-pick"] = cur_out
        st.selectbox("Destination Folder", opts, key="out-pick", on_change=_dest_changed,
                     format_func=lambda p: (f"{folder_label(out_default)}  (default)" if p == DEFAULT_OUT else
                                            folder_label(p)))
        if st.session_state.get("out-typing"):
            st.text_input("Folder path", key="out-typed", on_change=_dest_typed, placeholder="/data/Output/Shoot")
        save_report = st.checkbox("Also save a CSV report there", False, key="fd-report")
    out_dir = Path(st.session_state.get("out-path") or out_default)
    return in_dir, out_dir, recursive, save_report


def apply_link_folder() -> bool:
    """Open straight into a folder from a link, e.g. http://localhost:8501/batch?folder=/Users/me/Shoot
    (used by the Finder Quick Action)."""
    folder = st.query_params.get("folder")
    if folder and st.session_state.get("_link_folder") != folder:
        st.session_state["_link_folder"] = folder
        _set_input(folder)
    return bool(folder)


@st.cache_data(max_entries=1500, ttl=3600, show_spinner=False)
def preview_item(path: str, mtime: float, dims, s: Settings, box):
    """Fast preview from a reduced decode. Returns (thumb JPEG, auto_box, label); boxes in full-res pixels."""
    img, _, _ = core.load(path, draft=1000)
    k = img.width / dims[0]
    face = core.detect_face(img) if s.anchor == "face" else None
    face = tuple(v / k for v in face) if face else None
    pad = fill_pads(dims, s)
    auto = core.auto_box(dims, s.crop.ratio, s.anchor, face, s.head_frac, pad)
    label = core.crop_label(dims, s, face, pad)
    use = core.fit_ratio(box, s.crop.ratio, dims, pad) if box else auto
    try:
        if s.ai_extend and not s.white_bg and core.top_edge_busy(img, tuple(v * k for v in use)):
            label += ", check hair"
        thumb = core.render_crop(img, tuple(v * k for v in use), s, (240, 360), False,
                                 mask_fn_for(s), f"{path}|{mtime}", extend_fn_for(s))
    except Exception as e:                       # AI not available: show the plain crop and say why
        thumb = core.render_crop(img, tuple(v * k for v in use), s, (240, 360), effects=False, fill="checker")
        label += f", AI error: {e}"
    buf = io.BytesIO()
    thumb.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue(), auto, label


@st.fragment
def batch_editor(src: Source, s: Settings):
    """Crop editor for one photo of the batch, with the result next to it."""
    if st.session_state.get("fd-adjust") != src.id:   # Done was pressed: close the editor (whole page)
        st.rerun(scope="app")
    store = adjustments()
    mtime = Path(src.id).stat().st_mtime
    key = adj_key(src.id, src.dims, s.crop.ratio)
    _, auto, _label = preview_item(src.id, mtime, src.dims, s, None)
    pad = fill_pads(src.dims, s)
    with st.container(border=True, key="fd-editor"):
        h1, h2 = st.columns([4, 1], vertical_alignment="center")
        h1.markdown(f"**Adjusting {src.filename}**")
        h2.button("Done", key="fd-done", type="primary", width="stretch", on_click=st.session_state.pop,
                  args=("fd-adjust", None))
        left, right = st.columns(2, gap="large")
        with left:
            guide = EYE_Y if (s.crop.slug == "profile" and s.anchor == "face") else None
            height = crop_editor(key, src.id, mtime, src.id, src.dims, auto, s.crop.ratio, pad,
                                 s.bg_color if s.white_bg else None, guide)
        with right:
            img = work_image(src.id, mtime, src.id)[0]
            k = img.width / src.dims[0]
            box = core.fit_ratio(store.get(key) or auto, s.crop.ratio, src.dims, pad)
            with st.spinner("Rendering..."):
                try:
                    result = core.render_crop(img, tuple(v * k for v in box), s, (560, height), False,
                                              mask_fn_for(s), f"{src.id}|{mtime}", extend_fn_for(s))
                except Exception as e:
                    st.error(str(e))
                    return
            buf = io.BytesIO()
            result.convert("RGB").save(buf, "JPEG", quality=90)
            st.markdown(preview_html(buf.getvalue(), "image/jpeg", height), unsafe_allow_html=True)
            st.caption("Result (preview quality). Adjustments are saved and used when you convert. Adjusted "
                       "photos are always converted again, even if the file already exists.")


def review(keep: list[Source], s: Settings, seen: dict, fps: dict):
    """Image previews with filters; click a picture to adjust its crop."""
    store = adjustments()
    can_adjust = bool(s.crop.ratio)
    st.subheader("Image Previews", anchor="image-previews")
    filters = FILTERS if can_adjust else [f for f in FILTERS if f not in ("Needs attention", "No face found",
                                                                       "Adjusted", "Not adjusted")]
    sorts = SORTS if can_adjust else SORTS[:-1]
    show = st.pills("Show", filters, default="All", key="fd-filter", label_visibility="collapsed") or "All"
    c1, c2, c3 = st.columns([3, 2, 2], vertical_alignment="center")
    query = c1.text_input("Search", key="fd-search", placeholder="Search file names",
                          label_visibility="collapsed").strip().lower()
    order = c2.selectbox("Sort", sorts, key="fd-sort", label_visibility="collapsed")
    if show not in filters:
        show = "All"

    def info(src):
        key = adj_key(src.id, src.dims, s.crop.ratio)
        box = store.get(key)
        mtime = Path(src.id).stat().st_mtime
        return dict(src=src, box=box, mtime=mtime, pre=None)

    rows = [info(x) for x in keep if not query or query in x.filename.lower()]
    need_labels = show in ("Needs attention", "No face found") or order == "Needs attention first"
    if need_labels:
        with st.spinner("Checking crops..."):
            for r in rows:
                r["pre"] = preview_item(r["src"].id, r["mtime"], r["src"].dims, s,
                                        tuple(r["box"]) if r["box"] else None)

    def attention(r):
        return friendly_label(r["pre"][2])[1] and not r["box"]

    test = {"All": lambda r: True,
            "Needs attention": attention,
            "No face found": lambda r: r["pre"][2].startswith("no face") and not r["box"],
            "Adjusted": lambda r: r["box"] is not None,
            "Not adjusted": lambda r: r["box"] is None,
            "Processed before": lambda r: bool(seen.get(fps.get(r["src"].id))),
            "New": lambda r: not seen.get(fps.get(r["src"].id))}[show]
    rows = [r for r in rows if test(r)]
    key_fn = {"Name (A-Z)": lambda r: r["src"].filename.lower(),
              "Name (Z-A)": lambda r: r["src"].filename.lower(),
              "Newest first": lambda r: -r["mtime"],
              "Oldest first": lambda r: r["mtime"],
              "Largest first": lambda r: -r["src"].nbytes,
              "Needs attention first": lambda r: (not attention(r), r["src"].filename.lower())}[order]
    rows.sort(key=key_fn, reverse=order == "Name (Z-A)")

    n_adj = sum(1 for x in keep if adj_key(x.id, x.dims, s.crop.ratio) in store)
    if not rows:
        st.info("No photos match this filter." if (show != "All" or query) else "No photos to preview.")
        return
    pages = (len(rows) - 1) // PER_PAGE + 1
    i1, i2 = st.empty(), c3
    page = i2.selectbox("Page", range(1, pages + 1), key="fd-page", label_visibility="collapsed",
                        format_func=lambda p: f"Page {p} of {pages}") if pages > 1 else 1
    page = min(page, pages)
    first = (page - 1) * PER_PAGE
    i1.caption(f"Showing {first + 1}-{min(first + PER_PAGE, len(rows))} of {len(rows)}"
               + (f" (filtered from {len(keep)})" if len(rows) != len(keep) else "")
               + (f" · {n_adj} adjusted by hand" if n_adj else "")
               + (" · Click a photo to adjust its crop." if can_adjust else ""))

    sel = st.session_state.get("fd-adjust")
    by_id = {x.id: x for x in keep}
    if can_adjust and sel in by_id:
        batch_editor(by_id[sel], s)

    items = []
    for r in rows[first: first + PER_PAGE]:
        src, box = r["src"], r["box"]
        thumb, _auto, label = r["pre"] or preview_item(src.id, r["mtime"], src.dims, s, tuple(box) if box else None)
        items.append(dict(img=thumb, title=src.filename, id=src.id, manual=bool(box), seen=seen.get(fps.get(src.id)),
                          label=label))
    thumb_grid(items, "fd-grid", s, adjust_state_key="fd-adjust" if can_adjust else None, min_px=130,
               scroll_to=".st-key-fd-editor")


def run_batch(jobs: list[Source], s: Settings, out_dir: Path, skipped, save_report, fps):
    store = dict(adjustments())
    folder = out_dir / s.crop.slug

    def work(src: Source):
        box = store.get(adj_key(src.id, src.dims, s.crop.ratio))
        with SLOTS:
            o = core.make_crop(src.id, src.filename, s, src.base, tuple(box) if box else None, src.nbytes,
                               mask_fn_for(s), f"{src.id}|{os.path.getmtime(src.id)}", extend_fn_for(s))
        if o.data:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / o.filename).write_bytes(o.data)
            o.data = None                          # free memory, the file is on disk
        return src, o

    t0, outputs, rows = time.time(), [], []
    prog = st.progress(0.0, text=progress_note(s, "Starting..."))
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(work, src) for src in jobs]
        for i, f in enumerate(as_completed(futures), 1):
            src, o = f.result()
            outputs.append(o)
            if o.status in ("ok", "over cap"):
                rows.append(hist_row("batch", src, fps.get(src.id, ""), o, s, str(folder / o.filename)))
            prog.progress(i / len(futures), text=f"Converted {i}/{len(futures)}: {o.source}")
    prog.empty()
    history.record(rows)
    release()
    outputs.sort(key=lambda o: o.source)
    report = None
    if save_report:
        out_dir.mkdir(parents=True, exist_ok=True)
        report = f"mosaic-report-{datetime.now():%Y%m%d-%H%M}.csv"
        (out_dir / report).write_bytes(core.report_csv(outputs + skipped))
    secs = time.time() - t0
    ai = "cut-out" if mask_fn_for(s) else "extend" if extend_fn_for(s) else "off"
    timing.log("batch", secs, detail=f"ai={ai}; {len(jobs)} photos; {WORKERS} workers; "
                                     f"{secs / max(1, len(jobs)):.2f} s per photo; {s.fmt} {s.crop.slug}")
    return dict(outputs=outputs, skipped=skipped, secs=secs, out=str(folder), report=report)


def batch_page():
    s = current_settings()
    go = st.session_state.pop("batch_go", False)              # Convert was pressed in the settings panel
    apply_link_folder()
    in_dir, out_dir, recursive, save_report = folder_pickers()

    if not in_dir.is_dir():
        if IN_DOCKER:
            st.error(f"Can't find `{in_dir}`. In Docker, only folders inside the shared folders can be used "
                     "(see INPUT_ROOT in docker/env.example).")
        else:
            st.error(f"Can't find `{in_dir}`. Choose another folder.")
        return

    pattern = in_dir.rglob("*") if recursive else in_dir.glob("*")
    paths = sorted(p for p in pattern if p.is_file() and p.suffix.lower() in core.INPUT_EXTS
                   and out_dir not in p.parents and not p.name.startswith("."))
    if not paths:
        st.info(f"No images found in `{in_dir}`."
                + ("" if recursive else " Tick 'Include subfolders' if they're in folders inside it."))
        return

    sources = []
    for p in paths:
        stt = p.stat()
        dims, err = probe_info(str(p), stt.st_mtime)
        sources.append(Source(str(p), p.name, stt.st_size, dims, error=err))
    keep, skipped = resolve_duplicates(sources, s, "fd")
    unreadable = [x for x in keep if not x.dims]
    keep = [x for x in keep if x.dims]
    fps = {x.id: fingerprint(x.id, os.path.getmtime(x.id), x.id) for x in keep}
    seen = history.recent([f for f in fps.values() if f], s.spec, session_id())

    store = adjustments()
    jobs, exists = [], []
    folder = out_dir / s.crop.slug
    for src in keep:
        size = core.planned_size(src.dims, s)
        redo = adj_key(src.id, src.dims, s.crop.ratio) in store         # hand-adjusted crops always re-run
        if not redo and core.output_exists(folder, src.base, s, s.crop.slug, size):
            exists.append(Output(src.filename, s.crop.slug, core.output_name(src.base, s, s.crop.slug, size), None,
                                 src.dims, None, src.nbytes, status="skipped", note="already exists"))
        else:
            jobs.append(src)
    skipped += exists + [Output(x.filename, "-", "", None, None, None, x.nbytes, status="error",
                                note=f"could not read file: {x.error}") for x in unreadable]
    if unreadable:
        reasons = sorted({x.error.split(" [")[0] for x in unreadable})
        st.error(f"{len(unreadable)} file(s) could not be read. " + " ".join(reasons))

    n_seen = sum(1 for x in keep if seen.get(fps.get(x.id)))
    st.markdown(f"**{len(sources)} images** found ({human(sum(x.nbytes for x in sources))}). "
                f"**{len(jobs)} files** to create" + (f", {len(exists)} already exist." if exists else "."))
    if n_seen:
        st.warning(f"{n_seen} of these photos were already processed with this format and size in the last "
                   f"{history.RECENT_DAYS} days (marked ! below). They will still be converted.")

    st.caption(f"Converted files go to `{short_path(str(folder))}`. Files that already exist there are skipped. "
               "Press **Convert** in the Output section on the left to start.")
    if go:
        if jobs:
            st.session_state.folder_result = run_batch(jobs, s, out_dir, skipped, save_report, fps)
            st.rerun()                                         # refresh the counts above
        else:
            st.info("Nothing to convert: every file already exists in the destination folder.")

    res = st.session_state.get("folder_result")
    if res:
        st.success(f"Finished in {res['secs']:.0f} s. Files saved to `{res['out']}`."
                   + (f" Report saved as `{res['report']}`." if res["report"] else ""))
        if native.IS_MAC:
            st.button("Show in Finder", key="fd-reveal", on_click=native.reveal, args=(res["out"],))
        for o in res["outputs"]:
            if o.status in ("error", "over cap"):
                (st.error if o.status == "error" else st.warning)(f"{o.source}: {o.note}")
        st.dataframe([o.row() for o in res["outputs"] + res["skipped"]], width="stretch", hide_index=True)

    if keep:
        st.divider()
        review(keep, s, seen, fps)

    st.divider()
    outputs = res["outputs"] if res else []
    metrics(len(sources), outputs, sum(x.nbytes for x in sources))
    if res:
        st.download_button("Download CSV report", core.report_csv(res["outputs"] + res["skipped"]),
                           f"mosaic-report-{datetime.now():%Y%m%d-%H%M}.csv", "text/csv", on_click="ignore")
    elif not keep:
        st.download_button("Download CSV report", core.report_csv(skipped),
                           f"mosaic-report-{datetime.now():%Y%m%d-%H%M}.csv", "text/csv", on_click="ignore")
