"""Image processing for Mosaic Photo-Editor. No interface code in here, so it can be reused
by a command-line tool or a native app.

Pipeline for one photo: load (reduced decode where possible) -> find the face -> work out the
crop box -> render (background replacement / AI extension) -> resize -> encode under a size cap.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from . import timing

Image.MAX_IMAGE_PIXELS = 200_000_000  # allow very large TIFFs / panoramas

try:  # HEIC / AVIF support
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

try:  # Face detection (OpenCV Haar cascade, bundled with opencv 4.x)
    import cv2
    import numpy as np

    _CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if _CASCADE.empty():
        _CASCADE = None
except Exception:  # pragma: no cover
    _CASCADE = None

YUNET_MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
FACE_DETECTION_AVAILABLE = _CASCADE is not None or YUNET_MODEL.exists()
INPUT_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".heic", ".heif", ".avif", ".webp"}
MIN_QUALITY = 40  # size cap will never push quality below this

# Output formats: key -> (Pillow format, extension, MIME type, lossy quality setting?)
FORMATS = {
    "webp": ("WEBP", ".webp", "image/webp", True),
    "jpeg": ("JPEG", ".jpg", "image/jpeg", True),
    "png": ("PNG", ".png", "image/png", False),
    "gif": ("GIF", ".gif", "image/gif", False),
}
FORMAT_LABELS = {"webp": "WebP", "jpeg": "JPEG", "png": "PNG", "gif": "GIF"}


# --------------------------------------------------------------------------- settings
@dataclass(frozen=True)
class CropPreset:
    name: str
    slug: str                       # used for file-name label and output sub-folder
    ratio: tuple[int, int]          # width : height
    max_size: tuple[int, int]       # larger images are reduced to this; smaller are left alone
    cap_kb: int                     # recommended file-size cap


CROP_PRESETS = [
    CropPreset("Profile headshot (2:3 portrait)", "profile", (2, 3), (800, 1200), 150),
    CropPreset("Landscape photo (3:2)", "landscape", (3, 2), (1200, 800), 200),
    CropPreset("Hero / banner (16:9)", "hero", (16, 9), (1920, 1080), 350),
    CropPreset("Social share (1.91:1)", "social", (1200, 630), (1200, 630), 200),
    CropPreset("Square (1:1)", "square", (1, 1), (1080, 1080), 120),
]

RECOMMENDED_CAPS = [
    ("Thumbnail / card (up to 600 px)", "30-80 KB"),
    ("Profile headshot (800x1200)", "100-150 KB"),
    ("Content / article image (1200 px)", "150-250 KB"),
    ("Hero / full-width banner (1920 px)", "250-400 KB"),
]


# Portrait framing, measured from a typical professional 800x1200 headshot with YuNet:
# face box = 38% of the frame height, eye line 32% down from the top.
HEAD_FRAC = 0.38
EYE_Y = 0.32
EXTEND_MAX = 0.25   # background colour: may add plain colour above the photo, up to 25% of its height


@dataclass(frozen=True)
class Settings:
    crop: CropPreset = CROP_PRESETS[0]
    anchor: str = "face"                        # face | top | center
    head_frac: float = HEAD_FRAC                # 0 = off; face height as share of crop height
    crop_cap_kb: int = 150
    quality: int = 82
    lossless: bool = False
    sharpen: bool = True
    keep_meta: bool = False
    clean_names: bool = True
    add_variant: bool = True
    add_dims: bool = False
    prefix: str = ""
    fmt: str = "webp"                           # webp | jpeg | png | gif
    white_bg: bool = False                      # profile only: AI background replacement
    extend_top: bool = False                    # with white_bg: allow plain headroom above the photo
    bg_color: str = "#FFFFFF"                   # replacement background colour
    ai_extend: bool = False                     # AI extender (generated backdrop), all shapes
    ext_top: float = 0.10                       # AI extender: max extra headroom, share of photo height
    ext_side: float = 0.0                       # AI extender: max extra width per side, share of width
    ext_bottom: float = 0.0                     # AI extender: max extra below, share of photo height

    @property
    def spec(self) -> str:
        """What the output looks like, for 'processed before' checks: format, shape and target size."""
        m = self.crop.max_size
        return f"{self.fmt}|{self.crop.slug}|{m[0]}x{m[1]}"

    @property
    def ext(self) -> str:
        return FORMATS[self.fmt][1]

    @property
    def mime(self) -> str:
        return FORMATS[self.fmt][2]

    @property
    def lossy(self) -> bool:
        """True when a quality setting applies (WebP lossy or JPEG)."""
        return FORMATS[self.fmt][3] and not (self.fmt == "webp" and self.lossless)


# --------------------------------------------------------------------------- naming
def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()


def base_name(filename: str, s: Settings) -> str:
    stem = Path(filename).stem
    if s.clean_names:
        stem = slugify(stem) or "image"
        prefix = slugify(s.prefix)
    else:
        prefix = s.prefix.strip()
    return f"{prefix}-{stem}" if prefix else stem


def output_name(base: str, s: Settings, variant: str, size: tuple[int, int]) -> str:
    name = base
    if s.add_variant:
        name += f"-{variant}"
    if s.add_dims:
        name += f"-{size[0]}x{size[1]}"
    return name + s.ext


# --------------------------------------------------------------------------- geometry
def oriented_size(img: Image.Image) -> tuple[int, int]:
    w, h = img.size
    try:
        if img.getexif().get(274, 1) in (5, 6, 7, 8):
            return h, w
    except Exception:
        pass
    return w, h


def probe(src: bytes | str | Path) -> tuple[int, int]:
    """Read dimensions from the file header only (fast)."""
    fp = io.BytesIO(src) if isinstance(src, bytes) else src
    with Image.open(fp) as im:
        return oriented_size(im)


def largest_box(size, ratio):
    W, H = size
    rw, rh = ratio
    if W * rh > H * rw:          # image is wider than target shape
        ch, cw = H, round(H * rw / rh)
    else:
        cw, ch = W, round(W * rh / rw)
    return min(cw, W), min(ch, H)


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def pad3(pad) -> tuple[int, int, int]:
    """Normalise an extension allowance to (top, side, bottom)."""
    if isinstance(pad, (int, float)):
        return int(pad), 0, 0
    pad = tuple(pad) + (0, 0, 0)
    return int(pad[0]), int(pad[1]), int(pad[2])


def pads(size, s) -> tuple[int, int, int]:
    """(top, side, bottom) pixels the crop may extend beyond the photo. Filled with the background
    colour (background replacement) or generated by the AI extender."""
    if not s.crop:
        return 0, 0, 0
    W, H = size
    top = side = bottom = 0.0
    if s.white_bg and s.extend_top:
        top = EXTEND_MAX * H
    if s.ai_extend:
        top, side, bottom = max(top, s.ext_top * H), s.ext_side * W, s.ext_bottom * H
    return int(round(top)), int(round(side)), int(round(bottom))


def is_extended(box, size) -> bool:
    l, t, r, b = box
    return l < 0 or t < 0 or r > size[0] or b > size[1]


def frame_face(size, ratio, face, head_frac, pad=(0, 0, 0)):
    """Portrait framing. face = (x, y, w, h, eye_y).
    Size: face height = head_frac of the frame. Zooms in on wide shots; with extension
    allowed (pad) it can also zoom out on tight shots. If it still can't reach the target,
    the head stays larger (no upper limit).
    Position: eye line fixed at EYE_Y from the top, face centred horizontally.
    pad = (top, side, bottom): how far the crop may go beyond the photo.
    Returns (box, zoomed, short_headroom, extended)."""
    W, H = size
    ptop, pside, pbot = pad3(pad)
    max_w, max_h = largest_box((W + 2 * pside, H + ptop + pbot), ratio)
    fh, eye = face[3], face[4]
    hf = head_frac or HEAD_FRAC
    c = EYE_Y / hf                    # eye-to-frame-top distance, in face heights, at standard framing
    target = fh / head_frac if head_frac else max_h
    ch = min(target, max_h)
    # The crop can't go below the photo (plus any allowed extension). If the standard frame
    # would, use a smaller frame that ends there: the head gets larger but keeps the same
    # space above it (never past 60% face height).
    bottom_limit = H + pbot
    if eye - max(EYE_Y * ch, c * fh) + ch > bottom_limit:
        ch = min(max(bottom_limit - eye + c * fh, fh / 0.6), max_h)
    zoomed = bool(head_frac) and abs(ch - target) < 1
    ch = int(round(ch))
    cw = int(round(ch * ratio[0] / ratio[1]))
    if cw > max_w:
        cw, ch = max_w, int(round(max_w * ratio[1] / ratio[0]))
    left = face[0] + face[2] / 2 - cw / 2
    top = eye - max(EYE_Y * ch, c * fh)            # eye line at EYE_Y, or lower when the head is larger
    short = top < -ptop - 0.01 * ch                 # less room above the head than needed
    left = int(round(min(max(-pside, left), W + pside - cw)))
    top = int(round(min(max(-ptop, top), bottom_limit - ch)))
    box = (left, top, left + cw, top + ch)
    return box, zoomed, short, is_extended(box, size)


def auto_box(size, ratio, anchor, face=None, head_frac=0.0, pad=(0, 0, 0)):
    """Automatic crop. Without a face: the largest crop of the target shape, using any allowed
    extension (pad) so less of the photo has to be cut off to fit the shape."""
    W, H = size
    if anchor == "face" and face:
        return frame_face(size, ratio, face, head_frac, pad)[0]
    ptop, pside, pbot = pad3(pad)
    cw, ch = largest_box((W + 2 * pside, H + ptop + pbot), ratio)
    left = (W - cw) / 2
    top = (H - ch) * (0.15 if anchor in ("top", "face") else 0.5)    # face falls back to top-weighted
    left = int(round(min(max(-pside, left), W + pside - cw)))
    top = int(round(min(max(-ptop, top), H + pbot - ch)))
    return left, top, left + cw, top + ch


def crop_label(size, s, face, pad=None):
    """Short label for previews and the report."""
    pad = pads(size, s) if pad is None else pad
    if s.anchor != "face" or not face:
        label = {"top": "top", "center": "centre"}.get(s.anchor, "no face, top")
        if s.anchor == "face":
            label = "no face, top"
        if is_extended(auto_box(size, s.crop.ratio, s.anchor, None, 0, pad), size):
            label += ", extended"
        return label
    _, zoomed, short, extended = frame_face(size, s.crop.ratio, face, s.head_frac, pad)
    label = "zoomed" if zoomed else ("close-up" if s.head_frac else "face")
    if short:
        label += ", needs headroom"
    if extended:
        label += ", extended"
    return label


def top_edge_busy(img: Image.Image, box) -> bool:
    """True when the crop extends above the photo and something other than backdrop (usually hair)
    touches the photo's top edge inside the crop. Generated fill there needs a check."""
    l, t, r, b = (int(round(v)) for v in box)
    if t >= 0:
        return False
    strip = img.crop((max(l, 0), 0, min(r, img.width), max(4, min(24, img.height // 40))))
    strip.thumbnail((400, 400))
    a = np.asarray(strip.convert("RGB"), dtype=np.float32)
    med = np.median(a.reshape(-1, 3), 0)
    return float((np.abs(a - med).max(axis=2) > 40).mean()) > 0.03


def fit_ratio(box, ratio, size, pad=(0, 0, 0)):
    """Snap a hand-drawn box to the exact ratio, shrinking around its centre and keeping it
    inside the photo plus the allowed extension (pad = (top, side, bottom))."""
    ptop, pside, pbot = pad3(pad)
    l, t, r, b = box
    W, H = size
    Wp, Hp = W + 2 * pside, H + ptop + pbot
    tt, bb = t + ptop, b + ptop                      # in the extended area's coordinates
    if bb > Hp and 0 <= tt < Hp - 16:
        # Pushed below the bottom (e.g. dragged down or "Headroom -" when the frame already sits on the
        # photo's bottom edge): keep the top edge where it was put and shrink from below, so the
        # headroom really gets smaller instead of the frame snapping back.
        ch = Hp - tt
        cw = ch * ratio[0] / ratio[1]
        if cw > Wp:
            cw, ch = Wp, Wp * ratio[1] / ratio[0]
        cw, ch = int(round(cw)), int(round(ch))
        cx = (l + r) / 2 + pside
        left = int(round(min(max(0, cx - cw / 2), Wp - cw))) - pside
        top = int(round(tt)) - ptop
        return left, top, left + cw, top + ch
    cw, ch = largest_box((max(1, min(r - l, Wp)), max(1, min(b - t, Hp))), ratio)
    cx, cy = (l + r) / 2 + pside, (t + b) / 2 + ptop
    left = int(round(min(max(0, cx - cw / 2), Wp - cw))) - pside
    top = int(round(min(max(0, cy - ch / 2), Hp - ch))) - ptop
    return left, top, left + cw, top + ch


def scaled_size(size, max_size):
    w, h = size
    mw, mh = max_size
    if w <= mw and h <= mh:
        return w, h
    k = min(mw / w, mh / h)
    return max(1, round(w * k)), max(1, round(h * k))


def output_exists(folder: Path, base: str, s: Settings, variant: str, size) -> bool:
    """Skip check. With head-size zoom the crop size depends on the face, so when
    dimensions are in the name, match any size."""
    if s.add_dims and s.anchor == "face" and s.head_frac:
        stem = output_name(base, replace(s, add_dims=False), variant, size)[:-len(s.ext)]
        return any(folder.glob(f"{stem}-*x*{s.ext}"))
    return (folder / output_name(base, s, variant, size)).exists()


def planned_size(src_size, s: Settings) -> tuple[int, int]:
    """Output dimensions without decoding the image (used for names and skip checks)."""
    return scaled_size(largest_box(src_size, s.crop.ratio), s.crop.max_size)


def fingerprint(src: bytes | str | Path) -> str:
    """Fast identity of a photo, the same whether it was uploaded or read from a folder:
    file size plus the first and last 64 KB. Used for the 'processed before' history."""
    chunk = 65536
    h = hashlib.sha1()
    if isinstance(src, (bytes, bytearray)):
        h.update(str(len(src)).encode())
        h.update(src[:chunk])
        h.update(src[-chunk:])
    else:
        size = os.path.getsize(src)
        h.update(str(size).encode())
        with open(src, "rb") as fh:
            h.update(fh.read(chunk))
            fh.seek(max(0, size - chunk))
            h.update(fh.read(chunk))
    return h.hexdigest()


# --------------------------------------------------------------------------- image ops
DETECT_SIZE = 1280   # px: face detection runs at this size, so framing never needs more


def load(src: bytes | str | Path, draft: int | None = None):
    """Decode, rotate by EXIF and normalise the colour mode. With `draft`, JPEGs are decoded
    directly at a reduced scale (1/2, 1/4, 1/8) no smaller than `draft` px: much faster and
    uses a fraction of the memory of a full decode."""
    img = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src)
    if draft and img.format == "JPEG":
        w, h = img.size
        if img.getexif().get(274, 1) in (5, 6, 7, 8):
            w, h = h, w
        k = draft / max(w, h)
        if k < 1:
            img.draft("RGB", (max(1, int(img.size[0] * k)), max(1, int(img.size[1] * k))))
    img = ImageOps.exif_transpose(img)
    exif, icc = img.info.get("exif"), img.info.get("icc_profile")
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    if img.mode.startswith("I"):               # 16/32-bit greyscale TIFF
        img = img.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    img = img.convert("RGBA" if has_alpha else "RGB")
    return img, exif, icc


def detect_face(img: Image.Image):
    """Largest face as (x, y, w, h, eye_y) in image pixels, or None.
    Uses YuNet (finds eyes precisely); falls back to the Haar cascade."""
    k = min(1.0, 1280 / max(img.size))
    small = img.convert("RGB").resize((max(1, round(img.width * k)), max(1, round(img.height * k))))
    if YUNET_MODEL.exists():
        try:
            bgr = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2BGR)
            det = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (bgr.shape[1], bgr.shape[0]), 0.7, 0.3, 50)
            _, faces = det.detect(bgr)
            if faces is not None and len(faces):
                f = max(faces, key=lambda r: r[2] * r[3])
                eye_y = (f[5] + f[7]) / 2
                return tuple(float(v / k) for v in (f[0], f[1], f[2], f[3], eye_y))
            return None
        except Exception:
            pass
    if _CASCADE is None:
        return None
    m = min(small.size)
    faces = _CASCADE.detectMultiScale(np.asarray(small.convert("L")), scaleFactor=1.1, minNeighbors=5,
                                      minSize=(max(24, m // 25), max(24, m // 25)))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return tuple(float(v / k) for v in (x, y, w, h, y + 0.41 * h))  # eyes ~41% down a Haar box


def resize_to(img, max_size, sharpen):
    target = scaled_size(img.size, max_size)
    if target == img.size:
        return img
    shrink = target[0] / img.width
    img = img.resize(target, Image.LANCZOS)
    if sharpen and shrink < 0.8:
        img = img.filter(ImageFilter.UnsharpMask(radius=1, percent=50, threshold=2))
    return img


def to_gif(img: Image.Image) -> Image.Image:
    """256-colour palette for GIF, keeping simple transparency."""
    if img.mode == "RGBA":
        alpha = img.getchannel("A")
        pal = img.convert("RGB").quantize(255, dither=Image.Dither.FLOYDSTEINBERG)
        pal.paste(255, mask=alpha.point(lambda a: 255 if a < 128 else 0))
        pal.info["transparency"] = 255
        return pal
    return img.convert("RGB").quantize(256, dither=Image.Dither.FLOYDSTEINBERG)


def encode(img, s: Settings, cap_kb: int, exif=None, icc=None):
    """Encode in the chosen format. Returns (bytes, quality_used or None, over_cap).
    The size cap lowers quality for WebP/JPEG; PNG and GIF have no quality setting,
    so they are only flagged when over the cap."""
    pil_fmt = FORMATS[s.fmt][0]
    if s.fmt == "jpeg" and img.mode != "RGB":       # JPEG has no transparency: flatten on white
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.getchannel("A") if img.mode == "RGBA" else None)
        img = bg
    elif s.fmt == "gif":
        img = to_gif(img)

    def enc(q):
        buf = io.BytesIO()
        if s.fmt == "webp":
            kw = dict(quality=q, method=4, lossless=s.lossless)
        elif s.fmt == "jpeg":
            kw = dict(quality=q, optimize=True, progressive=True)
        elif s.fmt == "png":
            kw = dict(compress_level=6)
        else:
            kw = dict(optimize=True)
        if exif and s.fmt in ("webp", "jpeg", "png"):
            kw["exif"] = exif
        if icc and s.fmt != "gif":
            kw["icc_profile"] = icc
        img.save(buf, format=pil_fmt, **kw)
        return buf.getvalue()

    q0 = s.quality if s.lossy else None
    data = enc(s.quality)
    cap = cap_kb * 1024
    over = bool(cap_kb) and len(data) > cap
    if not s.lossy or not over:
        return data, q0, over
    lo, hi, best = MIN_QUALITY, s.quality - 1, None
    while lo <= hi:                          # binary search highest quality under the cap
        mid = (lo + hi) // 2
        d = enc(mid)
        if len(d) <= cap:
            best, lo = (d, mid), mid + 1
        else:
            hi = mid - 1
    if best:
        return best[0], best[1], False
    return enc(MIN_QUALITY), MIN_QUALITY, True


def over_cap_note(s: Settings, cap_kb: int) -> str:
    if s.lossy:
        return f"could not reach {cap_kb} KB at quality {MIN_QUALITY}"
    return f"over {cap_kb} KB ({FORMAT_LABELS[s.fmt]} has no quality setting to lower)"


def whiten_background(img: Image.Image, mask: Image.Image, color=(255, 255, 255)) -> Image.Image:
    """Replace the backdrop with `color` using an alpha mask (255 = person).
    Uses I + (1 - a) * (C - B), where B is the local backdrop colour, so semi-transparent
    hair edges lose the old backdrop tint instead of getting a halo."""
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
    a = np.asarray(mask.convert("L").resize(img.size, Image.BILINEAR), dtype=np.float32) / 255.0
    a = np.where(a < 0.02, 0.0, np.where(a > 0.98, 1.0, a))
    # local backdrop colour, estimated from confident background pixels at low resolution
    k = min(1.0, 384 / max(img.size))
    sw, sh = max(1, round(img.width * k)), max(1, round(img.height * k))
    small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
    w = (cv2.resize(a, (sw, sh), interpolation=cv2.INTER_AREA) < 0.1).astype(np.float32)
    if w.sum() < 10:                                  # almost no backdrop visible
        return img.convert("RGB")
    sigma = max(sw, sh) * 0.08
    num = cv2.GaussianBlur(small * w[..., None], (0, 0), sigma)
    den = cv2.GaussianBlur(w, (0, 0), sigma)[..., None]
    fallback = (small * w[..., None]).sum((0, 1)) / w.sum()
    bg = np.where(den > 1e-3, num / np.maximum(den, 1e-3), fallback)
    bg = cv2.resize(bg.astype(np.float32), img.size, interpolation=cv2.INTER_LINEAR)
    c = np.array(color, dtype=np.float32)
    out = rgb + (1.0 - a)[..., None] * (c - bg)
    out[a <= 0.0] = c                                  # pure background: exactly the chosen colour
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


SEAM_REGION = 0.30     # bottom extension only touches the lowest 30% of the frame (chest, below the collar)
SEAM_MAX = 0.35        # add at most this share of that region (~10% of the frame) - beyond it clothes stretch
SEAM_SPREAD = 3        # px: keep repeated rows apart so no band looks stretched


def _horizontal_seams(energy: np.ndarray, n: int) -> list[np.ndarray]:
    """Find n distinct low-energy horizontal seams (one row per column, original coordinates).
    Seams move up/down at most 1 px per column, so they slip between buttons, along folds and
    around seam lines instead of cutting through them."""
    h, w = energy.shape
    e = energy.astype(np.float32).copy()
    penalty = float(np.percentile(e, 90)) * 2 + 1.0
    rows = np.tile(np.arange(h)[:, None], (1, w))
    cols = np.arange(w)
    seams = []
    for _ in range(n):
        hh = e.shape[0]
        cost = e.copy()
        for x in range(1, w):                               # dynamic programming, column by column
            prev = cost[:, x - 1]
            cost[:, x] += np.minimum(prev, np.minimum(np.r_[np.inf, prev[:-1]], np.r_[prev[1:], np.inf]))
        seam = np.empty(w, dtype=np.int64)
        seam[-1] = int(np.argmin(cost[:, -1]))
        for x in range(w - 2, -1, -1):
            y = seam[x + 1]
            lo, hi = max(0, y - 1), min(hh, y + 2)
            seam[x] = lo + int(np.argmin(cost[lo:hi, x]))
        seams.append(rows[seam, cols])
        for d in range(-SEAM_SPREAD, SEAM_SPREAD + 1):     # discourage the next seams from bunching here
            e[np.clip(seam + d, 0, hh - 1), cols] += penalty
        keep = np.ones_like(e, dtype=bool)
        keep[seam, cols] = False
        e = e.T[keep.T].reshape(w, hh - 1).T
        rows = rows.T[keep.T].reshape(w, hh - 1).T
    return seams


def extend_bottom_content_aware(img: Image.Image, add: int) -> Image.Image:
    """Make a portrait `add` px taller at the bottom without generating anything: low-detail rows of
    the clothing (lowest 30% of the frame) are repeated along wavy seams that avoid buttons, seam
    lines, fabric folds and edges, so those stay crisp and only move slightly further apart.
    The face, neck and collar are never touched."""
    if add <= 0:
        return img
    a = np.asarray(img.convert("RGB"))
    H, W = a.shape[:2]
    y0 = int(H * (1 - SEAM_REGION))
    region = a[y0:]
    rh = region.shape[0]
    add = min(add, max(1, int(rh * SEAM_MAX)))
    gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY).astype(np.float32)
    energy = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)) + np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    seams = _horizontal_seams(energy, add)
    dup = np.zeros((rh, W), np.int32)
    xs = np.arange(W)
    for sm in seams:
        dup[sm, xs] += 1
    out = np.empty((rh + add, W, 3), np.uint8)
    reg16 = region.astype(np.uint16)
    nxt = np.concatenate([reg16[1:], reg16[-1:]], 0)
    blended = ((reg16 + nxt) // 2).astype(np.uint8)         # a repeated row blends into the next one
    for x in range(W):                                       # rebuild each column with its repeats
        idx = np.repeat(np.arange(rh), 1 + dup[:, x])
        first = np.r_[True, idx[1:] != idx[:-1]]
        out[:, x] = np.where(first[:, None], region[idx, x], blended[idx, x])
    return Image.fromarray(np.concatenate([a[:y0], out], 0), "RGB")


def checkerboard(size, cell=10) -> Image.Image:
    """Light checkerboard: marks areas the AI extender will generate."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    board = np.where(((xx // cell) + (yy // cell)) % 2 == 0, 236, 222).astype(np.uint8)
    return Image.fromarray(np.stack([board] * 3, -1), "RGB")


def _feather(size, edge) -> Image.Image:
    """Mask that is opaque inside and fades out over `edge` px at the border, so pasted photo
    pixels blend into generated surroundings without a visible seam."""
    w, h = size
    m = Image.new("L", (w, h), 0)
    e = max(1, int(edge))
    if w > 2 * e and h > 2 * e:
        ImageDraw.Draw(m).rectangle((e, e, w - 1 - e, h - 1 - e), fill=255)
        return m.filter(ImageFilter.GaussianBlur(e / 2))
    return Image.new("L", (w, h), 255)


def render_crop(img: Image.Image, box, s, out_size, sharpen=False, mask_fn=None, key="",
                extend_fn=None, effects=True, fill=None):
    """Crop `box` (image pixels; it may reach beyond the photo) and resize it to fit out_size.

    effects=True applies the AI work from the settings:
      - background replacement: mask_fn(img, region, key) -> person mask for that region;
        the backdrop becomes s.bg_color and any area beyond the photo is filled with it.
      - AI extender: extend_fn(img, (top, side, bottom), key) -> (canvas, scale): the photo with
        generated surroundings for the maximum allowed extension, made once per photo, so moving
        or zooming the crop never waits for the AI again.
      - Profile crops that reach below the photo: the clothing is lengthened content-aware.
    effects=False shows the plain photo (for before/after); areas beyond the photo are drawn
    in `fill` (a colour, or "checker")."""
    l, t, r, b = (int(round(v)) for v in box)
    W, H = img.size
    cw, ch = r - l, b - t
    k = min(1.0, out_size[0] / cw, out_size[1] / ch)
    out_w, out_h = max(1, round(cw * k)), max(1, round(ch * k))
    recolor = bool(effects and s.white_bg and mask_fn)
    generate = bool(effects and s.ai_extend and extend_fn and not recolor)
    lengthen = bool(effects and b > H and s.crop.slug == "profile" and (recolor or generate))
    mb = min(b, H) if lengthen else b                      # bottom of the part made by pasting/generating
    inside = (max(l, 0), max(t, 0), min(r, W), min(mb, H))
    real = img.crop(inside)
    if inside == (l, t, r, b) and not recolor:
        return resize_to(real, out_size, sharpen)
    if k < 1:
        real = real.resize((max(1, round(real.width * k)), max(1, round(real.height * k))), Image.LANCZOS)
    color = hex_to_rgb(s.bg_color)
    if recolor:
        real = whiten_background(real, mask_fn(img, inside, key), color)
    if sharpen and k < 0.8:
        real = real.filter(ImageFilter.UnsharpMask(radius=1, percent=50, threshold=2))
    main_h = max(1, round((mb - t) * k))
    off_x, off_y = round((inside[0] - l) * k), round((inside[1] - t) * k)
    if (off_x, off_y, real.width, real.height) == (0, 0, out_w, main_h):
        canvas = real                                    # nothing beyond the photo except below
    elif generate:
        ptop, pside, pbot = pad3(pads(img.size, s))
        portrait = s.crop.slug == "profile"          # portraits are lengthened below, never generated
        gen, g = extend_fn(img, (ptop, pside, 0 if portrait else pbot), key)
        gx0, gy0 = (l + pside) * g, (t + ptop) * g
        region = gen.crop((round(gx0), round(gy0), round(gx0 + cw * g), round(gy0 + (mb - t) * g)))
        canvas = region.resize((out_w, main_h), Image.LANCZOS).convert("RGB")
        canvas.paste(real, (off_x, off_y), _feather(real.size, max(2, 3 * min(real.size) // 400)))
    else:
        if fill == "checker":
            canvas = checkerboard((out_w, main_h))
        else:
            c = color if recolor else (hex_to_rgb(fill) if isinstance(fill, str) else
                                       tuple(int(v) for v in np.median(np.asarray(real).reshape(-1, 3), 0)))
            canvas = Image.new("RGB", (out_w, main_h), c)
        canvas.paste(real, (off_x, off_y))
    if lengthen and out_h > canvas.height:
        # portraits: lengthen the clothing content-aware instead of generating it (no smudging)
        canvas = extend_bottom_content_aware(canvas, out_h - canvas.height)
    if canvas.size != (out_w, out_h):
        canvas = canvas.resize((out_w, out_h), Image.LANCZOS)
    return canvas


# --------------------------------------------------------------------------- results
@dataclass
class Output:
    source: str
    variant: str
    filename: str
    data: bytes | None
    src_size: tuple[int, int] | None
    out_size: tuple[int, int] | None
    bytes_in: int
    bytes_out: int = 0
    quality: int | None = None
    crop: str = ""
    crop_box: tuple | None = None
    status: str = "ok"
    note: str = ""
    mime: str = "image/webp"

    @property
    def folder(self):
        return self.variant

    def row(self):
        saved = (1 - self.bytes_out / self.bytes_in) * 100 if self.bytes_in and self.bytes_out else None
        dims = lambda d: f"{d[0]}x{d[1]}" if d else ""
        return {
            "source": self.source,
            "output": f"{self.folder}/{self.filename}" if self.filename else "",
            "variant": self.variant,
            "source_dimensions": dims(self.src_size),
            "output_dimensions": dims(self.out_size),
            "source_kb": round(self.bytes_in / 1024, 1),
            "output_kb": round(self.bytes_out / 1024, 1) if self.bytes_out else None,
            "saved_pct": round(saved) if saved is not None else None,
            "quality": self.quality,
            "crop": self.crop,
            "status": self.status,
            "note": self.note,
        }


def make_crop(src, filename, s: Settings, base: str, manual_box=None, bytes_in=0,
              mask_fn=None, key="", extend_fn=None, loaded=None) -> Output:
    """Shape crop of one photo. Framing is worked out on a small decode; the photo is then
    decoded only as large as the output needs (JPEG). Pass `loaded` = (img, exif, icc) to reuse
    an image that is already decoded (it may be a reduced decode)."""
    t0 = time.perf_counter()
    try:
        full = probe(src)
        if loaded is None:
            small, exif, icc = load(src, draft=DETECT_SIZE)
        else:
            small, exif, icc = loaded
        k0 = small.width / full[0]
        can_fill = (s.white_bg and mask_fn) or (s.ai_extend and extend_fn)
        pad = pads(full, s) if can_fill else (0, 0)
        if manual_box:
            box, how = fit_ratio(manual_box, s.crop.ratio, full, pad), "manual"
        else:
            face = detect_face(small) if s.anchor == "face" else None
            face = tuple(v / k0 for v in face) if face else None
            box = auto_box(full, s.crop.ratio, s.anchor, face, s.head_frac, pad)
            how = crop_label(full, s, face, pad)
        target = scaled_size((box[2] - box[0], box[3] - box[1]), s.crop.max_size)
        need = min(1.0, 1.25 * target[0] / (box[2] - box[0]))     # resolution the output needs
        if k0 < need - 1e-6:
            img, exif, icc = load(src, draft=int(max(full) * need) + 1)
        else:
            img = small
        del small
        k = img.width / full[0]
        boxk = tuple(v * k for v in box)
        out = render_crop(img, boxk, s, target, s.sharpen, mask_fn, key or filename, extend_fn)
        if s.white_bg and mask_fn:
            how += f", background {s.bg_color.upper()}"
        elif s.ai_extend and extend_fn and is_extended(box, full):
            how = how.replace(", extended", "") + ", AI extended"
            if top_edge_busy(img, boxk):
                how += ", check hair"
        del img
        data, q, over = encode(out, s, s.crop_cap_kb, exif if s.keep_meta else None, icc)
        note = []
        if out.size[0] < s.crop.max_size[0]:
            note.append("smaller than target, kept source resolution")
        if over:
            note.append(over_cap_note(s, s.crop_cap_kb))
        ai = "cut-out" if (s.white_bg and mask_fn) else "extend" if (s.ai_extend and extend_fn) else "off"
        timing.log("convert" if loaded is None else "edit", time.perf_counter() - t0, filename,
                   full[0] * full[1] / 1e6, f"ai={ai}; {s.fmt} {s.crop.slug} {out.size[0]}x{out.size[1]}")
        return Output(filename, s.crop.slug, output_name(base, s, s.crop.slug, out.size), data,
                      full, out.size, bytes_in, len(data), q, how, box,
                      "over cap" if over else "ok", "; ".join(note), s.mime)
    except Exception as e:
        return Output(filename, s.crop.slug, "", None, None, None, bytes_in, status="error", note=str(e))


def release_memory():
    """Give freed memory back to the operating system after big jobs."""
    import ctypes
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)                       # Linux / Docker
    except Exception:
        try:
            ctypes.CDLL("/usr/lib/libSystem.B.dylib").malloc_zone_pressure_relief(None, 0)   # macOS
        except Exception:
            pass


def report_csv(outputs: list[Output]) -> bytes:
    rows = [o.row() for o in outputs]
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens it cleanly


# --------------------------------------------------------------------------- duplicates
@dataclass
class Source:
    id: str
    filename: str
    nbytes: int
    dims: tuple[int, int] | None
    base: str = ""
    duplicate_of: str | None = None   # exact duplicate (same name, dimensions and file size)
    error: str = ""                   # why the file could not be read

    @property
    def label(self):
        d = f"{self.dims[0]}x{self.dims[1]}" if self.dims else "?"
        return f"{self.filename}  ({d}, {self.nbytes / 1024 / 1024:.1f} MB)"


def find_duplicates(sources: list[Source], s: Settings):
    """Group by output name. Same name + same dimensions + same file size = exact duplicate (skipped).
    Same name but different dimensions or size = conflict for the user to resolve.
    Returns list of conflicts, each a list of Sources (largest resolution first)."""
    groups: dict[str, list[Source]] = {}
    for src in sources:
        src.base = base_name(src.filename, s)
        src.duplicate_of = None
        groups.setdefault(src.base.lower(), []).append(src)
    conflicts = []
    for items in groups.values():
        seen: dict[tuple, Source] = {}
        for src in items:
            sig = (src.dims, src.nbytes)
            if sig in seen:
                src.duplicate_of = seen[sig].filename
            else:
                seen[sig] = src
        reps = list(seen.values())
        if len(reps) > 1:
            reps.sort(key=lambda x: -(x.dims[0] * x.dims[1] if x.dims else 0))
            conflicts.append(reps)
    return conflicts


def apply_choice(conflict: list[Source], choice: str):
    """choice = id of the source to keep, or 'all' to keep every one with -2, -3 suffixes."""
    for i, src in enumerate(conflict):
        if choice == "all":
            if i:
                src.base = f"{src.base}-{i + 1}"
        elif src.id != choice:
            src.duplicate_of = f"name clash, kept {next(x.filename for x in conflict if x.id == choice)}"
