"""Normalised screen layout for the League of Legends HUD.

All rectangles are fractions of the *game client area* (0..1, origin top-left),
so the same layout works at 1920x1080, 2560x1440 or a windowed 1600x900 as long
as the aspect ratio is 16:9.  Values are deliberately generous: OCR reads a
whole slot and the parser finds the numbers inside it, so a few pixels of drift
do not break extraction.

``mode`` semantics
------------------
``line``  the rectangle is one text line; recognition runs without detection
          (roughly 2-6 ms per slot on a modern CPU).
``det``   full detection + recognition, used for unpredictable text such as
          centre-screen announcements.  Costs 200-500 ms, so it runs less often.
"""

from __future__ import annotations

import json
from pathlib import Path

# Basis resolution the defaults were measured against.
BASIS = (1920, 1080)

DEFAULT_LAYOUT: dict = {
    "version": 1,
    "basis": {"width": BASIS[0], "height": BASIS[1]},
    # Regions are drawn from the top-left of the game client area.
    "regions": {
        # Team kills + game clock live in the centred top bar.  Split into three
        # slots: recognition without detection is ~3 ms per slot, while letting
        # detection find the boundaries costs ~500 ms.
        "top": {
            "rect": [0.335, 0.000, 0.330, 0.048],
            "mode": "line",
            "everyMs": 700,
            "purpose": "game clock and team kills",
            "parts": {
                "ally": [0.00, 0.10, 0.20, 0.80],
                "clock": [0.28, 0.00, 0.44, 1.00],
                "enemy": [0.80, 0.10, 0.20, 0.80],
            },
        },
        # Kill feed: right-aligned stack under the objective bar.
        "feed": {
            "rect": [0.620, 0.060, 0.370, 0.250],
            "mode": "line",
            "rows": 6,
            "gap": 0.06,
            "everyMs": 1200,
            "purpose": "kill feed lines",
        },
        # Centre announcements: epic monsters, shutdowns, "you have been slain".
        # Kept clear of the kill-feed column on the right.
        "center": {
            "rect": [0.255, 0.155, 0.415, 0.150],
            "mode": "det",
            "everyMs": 5000,
            "purpose": "announcements and notices",
        },
        # Tab scoreboard.  Detection is used because one table spans the screen.
        # Off by default: it costs ~0.5 s per read and is blank most of the time.
        # Set "enabled": true (or "mode": "line" for a cheaper row-slot read) when
        # you want KDA/CS for every player.
        "board": {
            "rect": [0.165, 0.120, 0.670, 0.720],
            "mode": "det",
            "rows": 12,
            "gap": 0.0,
            "everyMs": 2500,
            "enabled": False,
            "purpose": "scoreboard",
            "optional": True,
        },
    },
    # Tight rectangles for numeric fields that are cheap to poll often.
    "fields": {
        "gameTime": {"rect": [0.455, 0.000, 0.075, 0.042], "mode": "line", "everyMs": 500},
        "level": {"rect": [0.372, 0.905, 0.028, 0.040], "mode": "line", "everyMs": 1500},
    },
    # Optional colour-bar probes; disabled until calibrated.
    "bars": {
        "hp": {"rect": [0.334, 0.868, 0.196, 0.020], "enabled": False},
        "mana": {"rect": [0.334, 0.892, 0.196, 0.016], "enabled": False},
    },
}


def layout_path(root: Path) -> Path:
    return root / "vision" / "layout.json"


def load_layout(root: Path | str) -> dict:
    """Load ``vision/layout.json`` if present, otherwise the built-in defaults."""
    path = layout_path(Path(root))
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_LAYOUT))
    data = json.loads(path.read_text(encoding="utf-8"))
    return merge_layout(DEFAULT_LAYOUT, data)


def merge_layout(base: dict, overlay: dict) -> dict:
    """Overlay user values on the defaults without dropping unknown keys."""
    result = json.loads(json.dumps(base))
    for key, value in (overlay or {}).items():
        if key in ("regions", "fields", "bars") and isinstance(value, dict):
            result.setdefault(key, {})
            for name, entry in value.items():
                if isinstance(entry, dict) and isinstance(result[key].get(name), dict):
                    result[key][name] = {**result[key][name], **entry}
                else:
                    result[key][name] = entry
        else:
            result[key] = value
    return result


def save_layout(root: Path | str, layout: dict) -> Path:
    path = layout_path(Path(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def to_pixels(rect, size, offset=(0, 0)) -> tuple[int, int, int, int]:
    """Convert a normalised rect to absolute screen pixels (x, y, w, h)."""
    width, height = size
    x, y, w, h = rect
    return (
        int(round(offset[0] + x * width)),
        int(round(offset[1] + y * height)),
        max(1, int(round(w * width))),
        max(1, int(round(h * height))),
    )


def slot_rects(region: dict) -> list[list[float]]:
    """Expand one region into per-line rects (normalised).

    ``gap`` is a fraction of the region height, so the layout survives scaling.
    """
    rows = int(region.get("rows") or 1)
    if rows <= 1:
        return [list(region["rect"])]
    x, y, w, h = region["rect"]
    gap = float(region.get("gap") or 0.0) * h
    row_h = max(h * 0.02, (h - gap * (rows - 1)) / rows)
    return [[x, y + index * (row_h + gap), w, row_h] for index in range(rows)]


def part_rect(region: dict, part) -> list[float]:
    """Convert a part rect (fractions of the region) to screen fractions."""
    x, y, w, h = region["rect"]
    px, py, pw, ph = part
    return [x + px * w, y + py * h, pw * w, ph * h]
