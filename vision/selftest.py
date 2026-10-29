"""Offline self-test for the OCR pipeline.

Renders a synthetic 1920x1080 HUD at the positions declared by the layout,
runs the real sidecar against it, and checks the extracted fields.  This
validates capture -> OCR -> parse -> protocol, not the real game's geometry;
use ``calibrate.py`` with a real screenshot for that.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / "vision" / ".venv" / "Scripts" / "python.exe"
SAMPLES = ROOT / "vision" / "samples"

sys.path.insert(0, str(ROOT / "vision"))
from lolvision.layout import DEFAULT_LAYOUT, part_rect, slot_rects  # noqa: E402

SIZE = (1920, 1080)
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


def px(rect):
    x, y, w, h = rect
    return int(x * SIZE[0]), int(y * SIZE[1]), int(w * SIZE[0]), int(h * SIZE[1])


def draw_centre(draw, rect, text, size=30, fill=(235, 235, 235)):
    x, y, w, h = px(rect)
    box = draw.textbbox((0, 0), text, font=font(size))
    draw.text((x + (w - (box[2] - box[0])) / 2, y + (h - (box[3] - box[1])) / 2 - box[1]),
              text, font=font(size), fill=fill)


def draw_left(draw, rect, text, size=26, fill=(235, 235, 235)):
    x, y, w, h = px(rect)
    box = draw.textbbox((0, 0), text, font=font(size))
    draw.text((x + 6, y + (h - (box[3] - box[1])) / 2 - box[1]), text, font=font(size), fill=fill)


def build_hud(path: Path, feed_lines: list[str] | None = None) -> None:
    image = Image.new("RGB", SIZE, (24, 30, 28))
    draw = ImageDraw.Draw(image)
    regions = DEFAULT_LAYOUT["regions"]
    top = regions["top"]
    for name, text in (("ally", "15"), ("clock", "23:41"), ("enemy", "8")):
        draw_centre(draw, part_rect(top, top["parts"][name]), text, size=30)
    feed_lines = feed_lines or ["亚索 击杀了 劫", "李青 击杀了 阿狸", "薇恩 击杀了 盖伦"]
    for slot, text in zip(slot_rects(regions["feed"]), feed_lines):
        draw_left(draw, slot, text, size=26)
    draw_centre(draw, regions["center"]["rect"], "巨龙已被击杀", size=40, fill=(240, 210, 120))
    draw_left(draw, DEFAULT_LAYOUT["fields"]["level"]["rect"], "11", size=30)
    image.save(path)


def build_board(path: Path) -> None:
    image = Image.new("RGB", SIZE, (24, 30, 28))
    draw = ImageDraw.Draw(image)
    rows = [
        ("暗裔剑魔 亚托克斯", "5/2/7", "182"),
        ("盲僧 李青", "3/4/9", "121"),
        ("九尾妖狐 阿狸", "7/1/4", "198"),
        ("疾风剑豪 亚索", "2/6/3", "96"),
        ("圣枪游侠 卢锡安", "6/3/5", "210"),
        ("影流之主 劫", "1/5/2", "88"),
        ("蒸汽机器人 布里茨", "0/7/11", "34"),
        ("琴瑟仙女 娑娜", "2/3/12", "41"),
        ("德玛西亚之力 盖伦", "4/4/6", "150"),
        ("无极剑圣 易", "8/2/3", "175"),
    ]
    x0, y0, w, h = px(DEFAULT_LAYOUT["regions"]["board"]["rect"])
    row_h = h / (len(rows) + 1)
    for index, (name, kda, cs) in enumerate(rows):
        y = y0 + row_h * (index + 1)
        draw.text((x0 + 40, y), name, font=font(24), fill=(230, 230, 230))
        draw.text((x0 + 620, y), kda, font=font(24), fill=(230, 230, 230))
        draw.text((x0 + 820, y), cs, font=font(24), fill=(230, 230, 230))
    image.save(path)


def run_sidecar(image: Path, frames: int = 1, layout: Path | None = None) -> dict:
    command = [str(PYTHON), str(ROOT / "vision" / "main.py"), "--image", str(image),
               "--no-voice", "--frames", str(frames)]
    if layout:
        command += ["--layout", str(layout)]
    process = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
    )
    events = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    if process.returncode != 0:
        raise SystemExit(f"sidecar failed:\n{process.stderr[-2000:]}")
    ready = next((e for e in events if e["type"] == "ready"), {})
    frames_out = [e for e in events if e["type"] == "frame"]
    return {"ready": ready, "frames": frames_out, "errors": [e for e in events if e["type"] == "error"]}


def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    hud, board = SAMPLES / "synthetic-hud.png", SAMPLES / "synthetic-board.png"
    build_hud(hud)
    build_board(board)

    print("== HUD ==")
    result = run_sidecar(hud)
    print("ready:", {k: result["ready"].get(k) for k in ("version", "warmupMs", "layout")})
    if result["errors"]:
        print("errors:", result["errors"])
    frame = result["frames"][0]
    print("durationMs:", frame["durationMs"], "regions:", frame["regions"])
    print("fields:", frame["fields"])
    print("lines:", [line["text"] for line in frame["lines"]])

    fields = frame["fields"]
    checks = {
        "gameTime": fields.get("gameTime") == 23 * 60 + 41,
        "allyKills": fields.get("allyKills") == 15,
        "enemyKills": fields.get("enemyKills") == 8,
        "level": fields.get("level") == 11,
    }

    # Two consecutive frames: the first is a baseline, the second adds one kill.
    seq = SAMPLES / "sequence"
    seq.mkdir(exist_ok=True)
    for old in seq.glob("*.png"):
        old.unlink()
    build_hud(seq / "001.png", ["亚索 击杀了 劫", "李青 击杀了 阿狸"])
    build_hud(seq / "002.png", ["亚索 击杀了 劫", "李青 击杀了 阿狸", "薇恩 击杀了 盖伦"])
    print("== EVENT SEQUENCE ==")
    sequence = run_sidecar(seq, frames=2)
    per_frame = [[(e.get("KillerName"), e.get("VictimName")) for e in f["events"]] for f in sequence["frames"]]
    print("events per frame:", per_frame)
    checks["killEvents"] = len(per_frame) == 2 and per_frame[0] == [] and ("薇恩", "盖伦") in per_frame[1]

    print("== BOARD ==")
    # The scoreboard reader is off by default; enable it just for this check.
    board_layout = SAMPLES / "layout-board.json"
    board_layout.write_text(json.dumps({"regions": {"board": {"enabled": True}}}), encoding="utf-8")
    board_result = run_sidecar(board, layout=board_layout)
    board_frame = board_result["frames"][0]
    rows = (board_frame.get("board") or {}).get("rows") or []
    parsed = [row for row in rows if row.get("kills") is not None]
    print("rows:", len(rows), "with KDA:", len(parsed))
    print("sample:", parsed[0] if parsed else None)
    checks["scoreboard"] = len(parsed) >= 4

    print("\n== RESULT ==")
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
