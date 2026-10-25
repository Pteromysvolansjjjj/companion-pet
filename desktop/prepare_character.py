"""Derive a character-only alpha image; preserve the original and visible RGB pixels.

Requires Pillow (build-time only). The boundary follows the visible hair contour
where the original thought bubble touches it; no hidden hair is invented.
"""
from pathlib import Path
import hashlib
import json
from PIL import Image, ImageDraw, ImageChops
import numpy as np

root = Path(__file__).resolve().parent.parent
source = root / "assets" / "DSniang02.png"
destination = root / "assets" / "DSniang02-character.png"
original_bytes = source.read_bytes()
original = Image.open(source).convert("RGBA")
assert original.size == (1026, 1026)

# The lower-left path also excludes both original thought circles. At the upper
# contour this follows the hair, rather than making a horizontal cut through it.
contour = [
    (449, 1026), (449, 710), (500, 710), (500, 630),
    (577, 553), (615, 511), (641, 505), (648, 486),
    (650, 476), (658, 462), (670, 449), (688, 438),
    (708, 430), (729, 425), (750, 425), (773, 430),
    (792, 439), (807, 451), (818, 467), (823, 482),
    (826, 500), (850, 500), (850, 420), (1026, 420),
    (1026, 1026),
]
scale = 4
mask = Image.new("L", (1026 * scale, 1026 * scale), 0)
ImageDraw.Draw(mask).polygon([(x * scale, y * scale) for x, y in contour], fill=255)
mask = mask.resize(original.size, Image.Resampling.LANCZOS)
derived = original.copy()
derived.putalpha(ImageChops.multiply(original.getchannel("A"), mask))
# The supplied cutout contains pale rectangular remnants next to transparent
# pixels. Remove only pale regions connected to the outside background. Dark
# character outlines protect enclosed white headband, eye and clothing pixels.
pixels = np.array(derived)
rgb = pixels[:, :, :3].astype(np.int16)
pale = (rgb.min(axis=2) >= 218) & ((rgb.max(axis=2) - rgb.min(axis=2)) <= 32)
eligible = (pixels[:, :, 3] < 12) | pale
flood = Image.fromarray(eligible.astype(np.uint8) * 255, "L")
ImageDraw.floodfill(flood, (0, 0), 0, thresh=0)
outside_pale = eligible & (np.array(flood) == 0)
removed_visible_pixels = int(((pixels[:, :, 3] > 0) & outside_pale).sum())
pixels[outside_pale, 3] = 0
pixels[pixels[:, :, 3] == 0, :3] = 0  # Do not retain the old bubble in invisible RGB.
derived = Image.fromarray(pixels, "RGBA")
derived.save(destination)
assert source.read_bytes() == original_bytes
assert derived.crop((0, 0, 1026, 420)).getchannel("A").getbbox() is None
assert derived.crop((300, 520, 405, 605)).getchannel("A").getbbox() is None
assert derived.crop((405, 610, 485, 680)).getchannel("A").getbbox() is None
report = {
    "source": source.name,
    "sourceSha256": hashlib.sha256(original_bytes).hexdigest(),
    "derived": destination.name,
    "derivedSha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    "method": "hand-traced supersampled alpha mask plus outside-connected pale-background cleanup; visible RGB unchanged; fully transparent RGB cleared; no generative redraw",
    "originalPreserved": True,
    "bubbleAndThoughtCirclesRemoved": True,
    "outsideConnectedPalePixelsCleared": removed_visible_pixels,
    "contour": contour,
}
(root / "assets" / "DSniang02-character-provenance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"ok": True, "output": str(destination), "originalPreserved": True}))
