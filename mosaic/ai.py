"""AI features of Mosaic Photo-Editor, run inside the app on the Apple GPU (Metal / MPS) when
available, otherwise on an NVIDIA GPU or the CPU.

- BiRefNet-portrait (MIT): person cut-out masks, used to replace the background.
- LaMa / big-lama (Apache-2.0): fills in missing backdrop when a photo is extended.

Models are downloaded on first use into the app data folder (see paths.py), loaded only when an
AI feature is used, and can be unloaded to free memory (unload()). Results are cached per photo,
so moving or zooming a crop never waits for the AI again.

To try another cut-out model (for example a fine-tuned BiRefNet on Hugging Face), set
MOSAIC_AI_MODEL to its repository id.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import threading
import time
from collections import OrderedDict

import numpy as np
from PIL import Image

from . import timing
from .paths import MODELS_DIR

# Some BiRefNet ops are not implemented on Metal yet; let PyTorch run just those on the CPU.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

MODEL_ID = os.environ.get("MOSAIC_AI_MODEL", "ZhengPeng7/BiRefNet-portrait")
MODEL_REVISION = os.environ.get("MOSAIC_AI_MODEL_REVISION") or None
SIZE = int(os.environ.get("MOSAIC_AI_SIZE", "1024"))       # BiRefNet's native input size
MASK_MAX = 1536          # px, long side of the region sent to the cut-out model
LAMA_URL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
LAMA_MD5 = "e3aa4aaa15225a33ec84f9f4bc47e500"
LAMA_PATH = MODELS_DIR / "big-lama.pt"
LAMA_MAX = int(os.environ.get("MOSAIC_LAMA_MAX", "1280"))  # px, long side LaMa works at
EXT_MARGIN = 32          # px generated beyond each extended edge and trimmed: LaMa leaves marks at edges
CACHE_MAX = 120          # results kept per cache (compressed PNG, ~50-300 KB each)
# MOSAIC_AI_FAKE=1: stand-in "models" (an oval mask, repeated edge pixels) so the interface and the image
# pipeline can be developed and tested without downloading anything. Never use it for real work.
FAKE = os.environ.get("MOSAIC_AI_FAKE") == "1"

_lock = threading.RLock()                  # one inference at a time: Metal is not thread-safe
_model = None
_lama = None
_device = None
_stats = {"masks": 0, "extends": 0}
_mask_cache: OrderedDict[str, tuple[bytes, int]] = OrderedDict()
_ext_cache: OrderedDict[str, bytes] = OrderedDict()
_cache_lock = threading.Lock()
last_error = ""


class AIUnavailable(RuntimeError):
    pass


INSTALL_HINT = ("The AI packages are not installed. Run ./scripts/install.sh again and answer yes "
                "to the AI features, or see the README.")


def available() -> bool:
    """True when PyTorch and the model libraries can be imported (does not load any model)."""
    if FAKE:
        return True
    # find_spec only looks for the packages; importing PyTorch here would add seconds to app start
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers", "torchvision"))


def _torch():
    try:
        import torch
        return torch
    except Exception as e:
        raise AIUnavailable(INSTALL_HINT) from e


def pick_device() -> str:
    forced = os.environ.get("MOSAIC_AI_DEVICE")
    if forced:
        return forced
    torch = _torch()
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _free_gpu():
    """Return cached GPU memory to the system after each job."""
    try:
        torch = _torch()
        if _device == "mps":
            torch.mps.empty_cache()
        elif _device == "cuda":
            torch.cuda.empty_cache()
    except Exception:
        pass


# --------------------------------------------------------------------------- cut-out (BiRefNet)
def _load_birefnet():
    global _model, _device
    with _lock:
        if _model is None:
            torch = _torch()
            from transformers import AutoModelForImageSegmentation
            torch.set_float32_matmul_precision("high")
            model = AutoModelForImageSegmentation.from_pretrained(
                MODEL_ID, trust_remote_code=True, revision=MODEL_REVISION)
            _device = _device or pick_device()
            model.to(_device).eval()
            _model = model
    return _model


def predict_mask(img: Image.Image) -> Image.Image:
    """Alpha mask (L, 255 = person) the same size as img."""
    if FAKE:
        from PIL import ImageDraw, ImageFilter
        m = Image.new("L", img.size, 0)
        w, h = img.size
        ImageDraw.Draw(m).ellipse((w * .25, h * .1, w * .75, h * 1.3), fill=255)
        _stats["masks"] += 1
        return m.filter(ImageFilter.GaussianBlur(max(1, w // 200)))
    torch = _torch()
    from torchvision import transforms
    model = _load_birefnet()
    tf = transforms.Compose([
        transforms.Resize((SIZE, SIZE)),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])
    x = tf(img.convert("RGB")).unsqueeze(0).to(_device)
    with _lock, torch.no_grad():
        pred = model(x)[-1].sigmoid().float().cpu()[0, 0].numpy()
        del x
        _free_gpu()
    _stats["masks"] += 1
    mask = Image.fromarray(np.clip(pred * 255, 0, 255).astype(np.uint8), "L")
    return mask.resize(img.size, Image.BILINEAR)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def _put(cache, key, value):
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > CACHE_MAX:
            cache.popitem(last=False)


def _get(cache, key):
    with _cache_lock:
        v = cache.get(key)
        if v is not None:
            cache.move_to_end(key)
        return v


def _snap_region(region, size):
    """Grow the crop region by 15% each side and snap it outward to a 1/8 grid of the photo.
    Small moves and zooms of the crop then reuse the same mask; the margin also gives the model
    context around the person, which improves hair edges."""
    W, H = size
    l, t, r, b = region
    mx, my = 0.15 * (r - l), 0.15 * (b - t)
    gx, gy = W / 8, H / 8
    sl = max(0, math.floor((l - mx) / gx) * gx)
    st = max(0, math.floor((t - my) / gy) * gy)
    sr = min(W, math.ceil((r + mx) / gx) * gx)
    sb = min(H, math.ceil((b + my) / gy) * gy)
    return int(sl), int(st), int(math.ceil(sr)), int(math.ceil(sb))


def mask_for(img: Image.Image, region, key: str) -> Image.Image:
    """Person mask (L) for `region` (l, t, r, b in img pixels), the size of that region.
    Cached per photo and snapped region, independent of the decode size of img."""
    W, H = img.size
    snap = _snap_region(region, img.size)
    frac = tuple(round(v / d, 3) for v, d in zip(snap, (W, H, W, H)))
    ck = f"{key}|{frac}"
    sw, sh = snap[2] - snap[0], snap[3] - snap[1]
    need = min(MASK_MAX, max(sw, sh))
    hit = _get(_mask_cache, ck)
    if hit is not None and hit[1] >= 0.9 * need:
        mask = Image.open(io.BytesIO(hit[0]))
    else:
        part = img.crop(snap).convert("RGB")
        part.thumbnail((MASK_MAX, MASK_MAX), Image.LANCZOS)
        with timing.timed("cut-out", key.split("|")[0].rsplit("/", 1)[-1], part.width * part.height / 1e6):
            mask = predict_mask(part)
        _put(_mask_cache, ck, (_png(mask), max(mask.size)))
    mask = mask.resize((sw, sh), Image.BILINEAR)
    l, t, r, b = (int(round(v)) for v in region)
    return mask.crop((l - snap[0], t - snap[1], r - snap[0], b - snap[1]))


# --------------------------------------------------------------------------- extender (LaMa)
def _download_lama():
    import requests
    LAMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LAMA_PATH.with_suffix(".part")
    md5 = hashlib.md5()
    with requests.get(LAMA_URL, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
                md5.update(chunk)
    if md5.hexdigest() != LAMA_MD5:
        tmp.unlink(missing_ok=True)
        raise AIUnavailable("The AI extender model download was corrupted (checksum mismatch). Try again.")
    tmp.rename(LAMA_PATH)


def _load_lama():
    global _lama, _device
    with _lock:
        if _lama is None:
            torch = _torch()
            if not LAMA_PATH.exists():
                _download_lama()
            _device = _device or pick_device()
            model = torch.jit.load(str(LAMA_PATH), map_location="cpu")
            try:
                model = model.to(_device)
                dev = _device
            except Exception:                    # if the GPU can't take this model, use the CPU
                dev = "cpu"
            model.eval()
            _lama = (model, dev)
    return _lama


def extend_image(img: Image.Image, top: int, left: int, right: int, bottom: int) -> Image.Image:
    """img on a larger canvas, the new border areas generated by LaMa. Original pixels are kept."""
    if FAKE:
        import cv2
        a = cv2.copyMakeBorder(np.asarray(img.convert("RGB")), top, bottom, left, right, cv2.BORDER_REPLICATE)
        _stats["extends"] += 1
        return Image.fromarray(a, "RGB")
    m = EXT_MARGIN
    T, L, R, B = (v + m if v > 0 else 0 for v in (top, left, right, bottom))
    out = _lama_fill(img, T, L, R, B)
    x0, y0 = L - left, T - top
    return out.crop((x0, y0, x0 + img.width + left + right, y0 + img.height + top + bottom))


def _lama_fill(img: Image.Image, top: int, left: int, right: int, bottom: int) -> Image.Image:
    import cv2
    torch = _torch()
    model, dev = _load_lama()
    src = np.asarray(img.convert("RGB"))
    H, W = src.shape[:2]
    canvas = cv2.copyMakeBorder(src, top, bottom, left, right, cv2.BORDER_REPLICATE)
    known = np.zeros(canvas.shape[:2], np.float32)
    known[top:top + H, left:left + W] = 1.0
    CH, CW = canvas.shape[:2]
    k = min(1.0, LAMA_MAX / max(CH, CW))
    wh = (max(8, round(CW * k)), max(8, round(CH * k)))
    small = cv2.resize(canvas, wh, interpolation=cv2.INTER_AREA) if k < 1 else canvas
    hole = (cv2.resize(1 - known, wh, interpolation=cv2.INTER_NEAREST) if k < 1 else 1 - known) > 0.5
    hole = cv2.dilate(hole.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0   # overlap the seam slightly
    ph, pw = (-small.shape[0]) % 8, (-small.shape[1]) % 8                       # LaMa wants multiples of 8
    x = np.pad(small, ((0, ph), (0, pw), (0, 0)), mode="reflect")
    mk = np.pad(hole.astype(np.float32), ((0, ph), (0, pw)), mode="reflect")
    xt = torch.from_numpy(x).permute(2, 0, 1)[None].float().div(255).to(dev)
    mt = torch.from_numpy(mk)[None, None].float().to(dev)
    with _lock, torch.no_grad():
        out = model(xt, mt)[0].permute(1, 2, 0).clamp(0, 1).mul(255).float().cpu().numpy()
        del xt, mt
        _free_gpu()
    _stats["extends"] += 1
    out = out[: small.shape[0], : small.shape[1]]
    if k < 1:
        out = cv2.resize(out, (CW, CH), interpolation=cv2.INTER_CUBIC)
    if top > 0:
        out = _smooth_top(out, src, top, left, W)
    blend = cv2.GaussianBlur(1 - known, (0, 0), 2.0)
    blend = np.maximum(blend, 1 - known)[..., None]
    result = canvas.astype(np.float32) * (1 - blend) + out * blend
    return Image.fromarray(np.clip(result, 0, 255).astype(np.uint8), "RGB")


def _smooth_top(out, src, top, left, W, near=24, fade=60):
    """Studio backdrops are smooth: far above the photo, fade the generated pixels into a clean
    continuation of the backdrop colour. Removes the streaks LaMa can leave in large empty areas."""
    import cv2
    CW = out.shape[1]
    rows = src[: max(4, min(24, src.shape[0] // 20))].astype(np.float32)        # top strip of the photo
    med = np.median(rows.reshape(-1, 3), 0)
    is_bg = np.abs(rows - med).max(axis=2) < 28                                 # ignore hair touching the edge
    weight = is_bg.sum(0).astype(np.float32)
    colsum = (rows * is_bg[..., None]).sum(0)
    k = max(9, W // 6)
    num = cv2.GaussianBlur(colsum[None], (0, 0), sigmaX=k / 3)[0]
    den = cv2.GaussianBlur(weight[None], (0, 0), sigmaX=k / 3)[0][:, None]
    color = np.where(den > 1e-3, num / np.maximum(den, 1e-3), med)
    color = np.concatenate([np.repeat(color[:1], left, 0), color,
                            np.repeat(color[-1:], CW - left - W, 0)])
    color = cv2.GaussianBlur(color[None].astype(np.float32), (0, 0), sigmaX=max(3, CW / 40))[0]
    d = (top - np.arange(top, dtype=np.float32))[:, None, None]
    w = np.clip((d - near) / fade, 0, 1)
    out = out.copy()
    out[:top] = out[:top] * (1 - w) + color[None] * w
    return out


def extended(img: Image.Image, pad, key: str):
    """The whole photo with generated surroundings for the maximum allowed extension
    pad = (top, side, bottom) in img pixels. Made once per photo and extension setting at
    LaMa's working resolution. Returns (canvas, scale): canvas pixels per img pixel; the canvas
    covers x from -side to W + side and y from -top to H + bottom."""
    W, H = img.size
    top, side, bottom = pad
    frac = (round(top / H, 2), round(side / W, 2), round(bottom / H, 2))   # same key at any decode size
    ck = f"{key}|{frac}"
    CW, CH = W + 2 * side, H + top + bottom
    g = min(1.0, LAMA_MAX / max(CW, CH))
    hit = _get(_ext_cache, ck)
    if hit is not None:
        gen = Image.open(io.BytesIO(hit)).convert("RGB")
    else:
        small = img.convert("RGB").resize((max(8, round(W * g)), max(8, round(H * g))), Image.LANCZOS)
        with timing.timed("extend", key.split("|")[0].rsplit("/", 1)[-1], small.width * small.height / 1e6,
                          f"pad top {frac[0]:.0%}, sides {frac[1]:.0%}, bottom {frac[2]:.0%}"):
            gen = extend_image(small, round(top * g), round(side * g), round(side * g), round(bottom * g))
        _put(_ext_cache, ck, _png(gen))
    return gen, gen.width / CW


# --------------------------------------------------------------------------- housekeeping
def status() -> dict:
    return {"available": available(), "device": _device, "warm": _warm["state"], "cutout_loaded": _model is not None,
            "extender_loaded": _lama is not None, **_stats}


def clear_caches():
    with _cache_lock:
        _mask_cache.clear()
        _ext_cache.clear()


def unload():
    """Release the AI models from memory (they load again on next use)."""
    global _model, _lama
    with _lock:
        _model = None
        _lama = None
        _free_gpu()
    _warm["state"] = "idle"


_warm = {"state": "idle"}            # idle | loading | ready | failed


def warm_state() -> str:
    return _warm["state"]


def warm_up_async(delay: float = 2.0):
    """Load both AI models in the background (after the interface has appeared), so the first
    background replacement or extension doesn't wait for them. Runs once per app start;
    Clear cached images unloads them again."""
    if _warm["state"] in ("loading", "ready") or not available():
        return

    def run():
        _warm["state"] = "loading"
        try:
            time.sleep(delay)
            t0 = time.time()
            predict_mask(Image.new("RGB", (64, 64), "grey"))          # loads the model and warms up the GPU
            if not FAKE:
                _load_lama()
            _stats["warmup_s"] = round(time.time() - t0, 1)
            timing.log("ai warm-up", time.time() - t0, detail="cut-out" + ("" if FAKE else " + extender"))
            _warm["state"] = "ready"
        except Exception as e:                                        # e.g. offline on first use
            _warm["state"] = "failed"
            _stats["warmup_error"] = f"{type(e).__name__}: {e}"
    threading.Thread(target=run, name="mosaic-ai-warmup", daemon=True).start()
