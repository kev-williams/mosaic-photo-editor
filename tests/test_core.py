"""Tests for the image pipeline. Images are generated in memory: no photos in the repository."""
import io
import os
from dataclasses import replace

os.environ.setdefault("MOSAIC_AI_FAKE", "1")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from mosaic import ai, core  # noqa: E402


def make_photo(w=1200, h=1600, fmt="JPEG") -> bytes:
    """A grey studio backdrop with a dark 'person' shape."""
    img = Image.new("RGB", (w, h), (205, 205, 210))
    d = ImageDraw.Draw(img)
    d.ellipse((w * .38, h * .18, w * .62, h * .48), fill=(190, 150, 130))     # head
    d.rectangle((w * .25, h * .5, w * .75, h), fill=(40, 50, 80))              # body
    noise = np.random.default_rng(1).integers(-12, 12, (h, w, 3))           # film grain, like a real photo
    img = Image.fromarray(np.clip(np.asarray(img, dtype=np.int16) + noise, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, fmt, quality=92)
    return buf.getvalue()


@pytest.fixture(scope="module")
def photo():
    return make_photo()


def test_fit_ratio_keeps_shape_and_bounds():
    box = core.fit_ratio((-50, -80, 700, 1500), (2, 3), (1200, 1600))
    l, t, r, b = box
    assert l >= 0 and t >= 0 and r <= 1200 and b <= 1600
    assert abs((r - l) / (b - t) - 2 / 3) < 0.01


def test_fit_ratio_with_extension_allows_space_above():
    box = core.fit_ratio((100, -200, 500, 400), (2, 3), (1200, 1600), pad=(300, 0, 0))
    assert box[1] < 0


def test_make_crop_profile_size_and_name(photo):
    s = core.Settings()
    o = core.make_crop(photo, "Jane DOE_01.JPG", s, core.base_name("Jane DOE_01.JPG", s))
    assert o.status == "ok", o.note
    assert o.filename == "jane-doe-01-profile.webp"
    w, h = o.out_size
    assert abs(w / h - 2 / 3) < 0.01 and h <= 1200
    assert Image.open(io.BytesIO(o.data)).format == "WEBP"


@pytest.mark.parametrize("fmt,pil", [("jpeg", "JPEG"), ("png", "PNG"), ("gif", "GIF")])
def test_formats(photo, fmt, pil):
    s = replace(core.Settings(), fmt=fmt)
    o = core.make_crop(photo, "a.jpg", s, "a")
    assert Image.open(io.BytesIO(o.data)).format == pil


def test_size_cap_lowers_quality(photo):
    s = replace(core.Settings(), crop_cap_kb=40, quality=95)
    o = core.make_crop(photo, "a.jpg", s, "a")
    assert o.bytes_out <= 40 * 1024 or o.status == "over cap"
    assert o.quality is not None and o.quality < 95


def test_background_replacement_uses_colour(photo):
    s = replace(core.Settings(), white_bg=True, bg_color="#FF0000")
    o = core.make_crop(photo, "a.jpg", s, "a", mask_fn=ai.mask_for, key="t1")
    img = np.asarray(Image.open(io.BytesIO(o.data)).convert("RGB"))
    corner = img[:10, :10].reshape(-1, 3).mean(0)
    assert corner[0] > 200 and corner[1] < 60 and corner[2] < 60


def test_ai_extender_generates_above(photo):
    s = replace(core.Settings(), ai_extend=True, ext_top=0.3, anchor="top", head_frac=0)
    full = core.probe(photo)
    pad = core.pads(full, s)
    box = core.fit_ratio((200, -pad[0], 800, 900), s.crop.ratio, full, pad)
    assert box[1] < 0
    o = core.make_crop(photo, "a.jpg", s, "a", manual_box=box, extend_fn=ai.extended, key="t2")
    assert o.status == "ok", o.note
    assert "AI extended" in o.crop


def test_before_view_marks_extension_with_checkerboard(photo):
    img, _, _ = core.load(photo)
    s = replace(core.Settings(), ai_extend=True)
    out = core.render_crop(img, (100, -300, 900, 900), s, (400, 600), effects=False, fill="checker")
    top = np.asarray(out)[:20]
    assert len(np.unique(top)) <= 3                      # just the two checkerboard greys


def test_fingerprint_same_for_bytes_and_file(tmp_path, photo):
    p = tmp_path / "x.jpg"
    p.write_bytes(photo)
    assert core.fingerprint(photo) == core.fingerprint(str(p))


def test_duplicates():
    a = core.Source("1", "Jane.jpg", 100, (10, 10))
    b = core.Source("2", "jane.JPG", 100, (10, 10))
    c = core.Source("3", "Jane.png", 200, (20, 20))
    conflicts = core.find_duplicates([a, b, c], core.Settings())
    assert b.duplicate_of == "Jane.jpg"
    assert len(conflicts) == 1
