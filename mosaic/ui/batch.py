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
from ..paths import IN_DOCKER, INPUT_DIR, OUTPUT_DIR, RECENT_FILE, read_json, write_json
from .common import (adj_key, progress_note, adjustments, crop_editor, current_settings, extend_fn_for, fill_pads,
                     fingerprint, format_picker, hist_row, human, mask_fn_for, metrics, preview_html, release,
                     resolve_duplicates, session_id, thumb_grid, work_image)

WORKERS = int(os.environ.get("WORKERS", min(4, os.cpu_count() or 2)))
SLOTS = threading.BoundedSemaphore(int(os.environ.get("FULL_SIZE_WORKERS", 2)))   # caps peak memory
SKIP_DIRS = (".photoslibrary", ".app", ".lrdata", ".fcpbundle", ".imovielibrary", ".tvlibrary", ".musiclibrary")
BROWSE_PLACEHOLDER = "Folders in Input..."
RECENT_PLACEHOLDER = "Recent folders..."


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


@st.cache_data(ttl=120, show_spinner="Looking for photo folders...")
def image_folders(root: str, depth: int = 4, limit: int = 400) -> list[tuple[str, int]]:
    """Folders under root that directly contain images, with counts. Skips app/library bundles."""
    out: list[tuple[str, int]] = []

    def walk(path: str, d: int):
        if len(out) >= limit:
            return
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
        except OSError:
            return
        n = sum(1 for e in entries if not e.name.startswith(".") and e.is_file()
                and os.path.splitext(e.name)[1].lower() in core.INPUT_EXTS)
        if n:
            out.append((path, n))
        if d:
            for e in entries:
                if (e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
                        and not e.name.lower().endswith(SKIP_DIRS)):
                    walk(e.path, d - 1)

    walk(root.rstrip("/") or "/", depth)
    return out


def load_recent() -> list[str]:
    return read_json(RECENT_FILE, [])[:8]


def save_recent(path: str):
    write_json(RECENT_FILE, ([path] + [p for p in load_recent() if p != path])[:8])


def normalize_path(raw: str) -> str:
    """Accept paths as copied from Finder, dragged into Terminal (escaped spaces) or quoted."""
    p = raw.strip().strip("'\"").replace("\\ ", " ")
    if p.startswith("file://"):
        p = unquote(p[7:])
    return os.path.expanduser(p).rstrip("/") or "/"


def _set_input(path: str):
    st.session_state["in-path"] = path
    st.session_state.pop("out-path", None)          # follow the input folder's name again


def _choose_input():
    p = native.pick_folder("Choose the folder with your photos", st.session_state.get("in-path") or str(INPUT_DIR))
    if p:
        _set_input(p)


def _choose_output():
    p = native.pick_folder("Choose where converted files go", str(OUTPUT_DIR))
    if p:
        st.session_state["out-path"] = p


def _use_pick(state_key, placeholder):
    pick = st.session_state.get(state_key)
    if pick and pick != placeholder:
        _set_input(pick)


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
    store = adjustments()
    mtime = Path(src.id).stat().st_mtime
    key = adj_key(src.id, src.dims, s.crop.ratio)
    _, auto, _label = preview_item(src.id, mtime, src.dims, s, None)
    pad = fill_pads(src.dims, s)
    with st.container(border=True):
        h1, h2 = st.columns([4, 1], vertical_alignment="center")
        h1.markdown(f"**Adjusting {src.filename}**")
        h2.button("Done", key="fd-done", width="stretch", on_click=st.session_state.pop, args=("fd-adjust", None))
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
                       "photos are always re-converted, even when 'Skip' is on.")


def review(keep: list[Source], s: Settings, seen: dict, fps: dict):
    """Crop review grid with per-photo adjustment."""
    store = adjustments()
    st.subheader("Review crops")
    c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    show = c1.radio("Show", ["All", "Needs attention", "No face found", "Adjusted", "Processed before"],
                    horizontal=True, key="fd-filter")
    per_page = 24
    rows = []
    with st.spinner("Preparing previews..."):
        for src in keep:
            key = adj_key(src.id, src.dims, s.crop.ratio)
            box = store.get(key)
            mtime = Path(src.id).stat().st_mtime
            if show == "All":
                rows.append((src, box, mtime, None))
                continue
            if show == "Processed before":
                if seen.get(fps.get(src.id)):
                    rows.append((src, box, mtime, None))
                continue
            pre = preview_item(src.id, mtime, src.dims, s, tuple(box) if box else None)
            label = pre[2]
            ok = {"Needs attention": ("headroom" in label or "check hair" in label) and not box,
                  "No face found": label.startswith("no face") and not box,
                  "Adjusted": box is not None}[show]
            if ok:
                rows.append((src, box, mtime, pre))
    if not rows:
        st.info("Nothing to show for this filter.")
        return
    pages = (len(rows) - 1) // per_page + 1
    page = c2.selectbox("Page", range(1, pages + 1), key="fd-page",
                        format_func=lambda p: f"Page {p} of {pages}") if pages > 1 else 1
    c3.caption(f"{len(rows)} photos · {sum(1 for x in keep if adj_key(x.id, x.dims, s.crop.ratio) in store)} adjusted")

    sel = st.session_state.get("fd-adjust")
    by_id = {x.id: x for x in keep}
    if sel in by_id:
        batch_editor(by_id[sel], s)

    items = []
    for src, box, mtime, pre in rows[(page - 1) * per_page: page * per_page]:
        thumb, _auto, label = pre or preview_item(src.id, mtime, src.dims, s, tuple(box) if box else None)
        items.append(dict(img=thumb, title=src.filename, id=src.id, manual=bool(box), seen=seen.get(fps.get(src.id)),
                          warn=("headroom" in label or "check hair" in label) and not box,
                          label="manual" if box else label))
    thumb_grid(items, 8, adjust_state_key="fd-adjust")


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
    apply_link_folder()
    roots = [str(INPUT_DIR)] + ([os.environ["INPUT_ROOT"]] if os.environ.get("INPUT_ROOT") else [])

    with st.container(border=True):
        st.markdown("**Photos to convert**")
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        recent = load_recent()
        if "in-path" not in st.session_state:
            st.session_state["in-path"] = recent[0] if recent else str(INPUT_DIR)
        c1.text_input("Folder", key="in-path", placeholder=f"{INPUT_DIR}")
        if native.can_pick():
            c2.button("Choose folder...", width="stretch", on_click=_choose_input, key="fd-choose-in")
        found = [f for r in roots for f in image_folders(r)]
        o1, o2 = st.columns(2)
        if found:
            counts = dict(found)
            o1.selectbox("Folders in Input", [BROWSE_PLACEHOLDER] + [p for p, _ in found], key="browse-pick",
                         on_change=_use_pick, args=("browse-pick", BROWSE_PLACEHOLDER), label_visibility="collapsed",
                         format_func=lambda p: p if p == BROWSE_PLACEHOLDER else
                         f"{os.path.relpath(p, os.path.dirname(str(INPUT_DIR)))}  ({counts[p]} images)")
        if len(recent) > 1:
            o2.selectbox("Recent", [RECENT_PLACEHOLDER] + recent, key="recent-pick", on_change=_use_pick,
                         args=("recent-pick", RECENT_PLACEHOLDER), label_visibility="collapsed")
        recursive = st.checkbox("Include subfolders", False, key="fd-recursive")

    raw = st.session_state.get("in-path", "")
    in_dir = Path(normalize_path(raw)) if raw.strip() else None
    out_default = OUTPUT_DIR / (in_dir.name if in_dir and in_dir != INPUT_DIR else "Batch")
    with st.container(border=True):
        st.markdown("**Converted files go to**")
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        out_raw = c1.text_input("Output folder", value=st.session_state.get("out-path") or str(out_default),
                                key=f"out-input-{st.session_state.get('out-path') or out_default}",
                                label_visibility="collapsed")
        if native.can_pick():
            c2.button("Choose folder...", width="stretch", on_click=_choose_output, key="fd-choose-out")
        e1, e2 = st.columns(2)
        on_exist = e1.radio("If a file already exists", ["Skip", "Overwrite"], horizontal=True, key="fd-exist")
        save_report = e2.checkbox("Also save a CSV report there", False, key="fd-report")
        st.caption(f"Files are saved in a '{s.crop.slug}' folder inside it.")
    format_picker()

    if in_dir is None:
        st.info("Choose a folder with photos.")
        return
    if not in_dir.is_dir():
        if IN_DOCKER:
            st.error(f"Can't find `{in_dir}`. In Docker, only folders inside the shared folders can be used "
                     "(see INPUT_ROOT in docker/env.example).")
        else:
            st.error(f"Can't find `{in_dir}`. Check the path for typos.")
        return
    save_recent(str(in_dir))
    out_dir = Path(normalize_path(out_raw))
    if not out_dir.is_absolute():
        out_dir = OUTPUT_DIR / out_dir

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
        if on_exist == "Skip" and not redo and core.output_exists(folder, src.base, s, s.crop.slug, size):
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

    if keep and st.button(f"Convert {len(jobs)} file{'s' if len(jobs) != 1 else ''}", type="primary",
                          disabled=not jobs, key="fd-convert"):
        st.session_state.folder_result = run_batch(jobs, s, out_dir, skipped, save_report, fps)

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
