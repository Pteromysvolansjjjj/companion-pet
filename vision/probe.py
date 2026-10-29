"""Inspect one screen frame: what the OCR sees, how it is parsed, where it lands.

Usage
    python vision/inspect.py                       # live: the game window
    python vision/inspect.py --image shot.png      # a screenshot instead
    python vision/inspect.py --image shot.png --save annotated.png

The annotated image draws every region rectangle on the frame, so layout.json
can be corrected by looking at one picture instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vision"))
os.environ["LOLVISION_NO_REDIRECT"] = "1"

from lolvision import layout as layout_mod  # noqa: E402
from lolvision.capture import find_windows  # noqa: E402


def draw_regions(image, layout, colour=(0, 255, 0)):
    height, width = image.shape[:2]
    for name, region in (layout.get("regions") or {}).items():
        x, y, w, h = layout_mod.to_pixels(region["rect"], (width, height))
        cv2.rectangle(image, (x, y), (x + w, y + h), colour, 2)
        cv2.putText(image, name, (x + 4, max(14, y + 16)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
        for index, slot in enumerate(layout_mod.slot_rects(region)):
            if index == 0:
                continue
            sx, sy, sw, sh = layout_mod.to_pixels(slot, (width, height))
            cv2.line(image, (sx, sy), (sx + sw, sy), (0, 200, 255), 1)
        for part_name, part in (region.get("parts") or {}).items():
            px, py, pw, ph = layout_mod.to_pixels(layout_mod.part_rect(region, part), (width, height))
            cv2.rectangle(image, (px, py), (px + pw, py + ph), (255, 128, 0), 1)
    for name, field in (layout.get("fields") or {}).items():
        x, y, w, h = layout_mod.to_pixels(field["rect"], (width, height))
        cv2.rectangle(image, (x, y), (x + w, y + h), (255, 0, 255), 1)
        cv2.putText(image, name, (x + 2, max(12, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1)
    return image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    parser.add_argument("--save", default="")
    parser.add_argument("--json", action="store_true", help="print the whole frame as JSON")
    args = parser.parse_args()

    from main import Vision  # the sidecar module owns the pipeline

    config = {"ocrEnabled": True, "requireForeground": False}
    if args.image:
        config["imagePath"] = args.image
    vision = Vision(ROOT, config)

    windows = find_windows()
    print("windows:", [(w.title, w.width, w.height, w.kind, w.foreground) for w in windows] or "none")

    frame = vision.capture_frame(force=True)
    if frame is None:
        print("没有取到画面：确认游戏在前台（无边框/窗口模式），或用 --image 指定截图。")
        return 1

    print("window:", frame["window"].get("title"), f'{frame["window"]["width"]}x{frame["window"]["height"]}')
    print("durationMs:", frame["durationMs"], "regions:", frame["regions"])
    print("fields:", json.dumps(frame["fields"], ensure_ascii=False))
    if frame.get("bars"):
        print("bars:", frame["bars"])
    print("lines:")
    for line in frame["lines"]:
        print(f'  [{line["region"]:<7}] {line["score"]:.2f}  {line["text"]}')
    if frame.get("board"):
        rows = frame["board"].get("rows") or []
        print(f"scoreboard: {len(rows)} rows")
        for row in rows[:12]:
            print("   ", row.get("text"))
    print("events:", json.dumps(frame["events"], ensure_ascii=False))
    if args.json:
        print(json.dumps(frame, ensure_ascii=False, indent=2))

    if args.save:
        if args.image:
            image = cv2.imread(args.image)
        else:
            from lolvision.capture import Grabber, select_window

            window = select_window(prefer_foreground=False)
            image = Grabber().grab(window.x, window.y, window.width, window.height) if window else None
        if image is not None:
            annotated = draw_regions(image.copy(), vision.layout)
            cv2.imwrite(args.save, annotated)
            print("saved:", args.save)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
