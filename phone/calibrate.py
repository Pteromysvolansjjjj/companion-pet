"""Check fields.yaml against a real screenshot.

The ROIs in ``phone/fields.yaml`` are estimates.  Take one screenshot of the
game, run this, and it tells you -- per field -- what OCR read, how confident it
was, and whether the rectangle actually covers the number.

    python phone/calibrate.py --shot shots/hud.png
    python phone/calibrate.py --shot shots/hud.png --out phone/calibration

Writes one crop per field plus an annotated overlay, so a wrong rectangle is
visible rather than guessed at.  Nothing is modified automatically: read the
numbers, edit fields.yaml, run again.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(HERE))

from lolvision.layout import to_pixels  # noqa: E402
from pc_capture import (HudReader, build_specs, load_config,  # noqa: E402
                        parse_value, prepare)


def main() -> int:
    parser = argparse.ArgumentParser(description="用真实截图校准 HUD 字段")
    parser.add_argument("--shot", required=True, help="游戏截图（整张画面，不要裁切）")
    parser.add_argument("--fields", default=str(HERE / "fields.yaml"))
    parser.add_argument("--profile", default="",
                        help="游戏档位名，读取 phone/profiles/<name>.yaml")
    parser.add_argument("--out", default=str(HERE / "calibration"))
    parser.add_argument("--json", action="store_true", help="额外输出机器可读结果")
    args = parser.parse_args()

    image = cv2.imread(str(Path(args.shot)))
    if image is None:
        print(f"读不到图片: {args.shot}")
        return 2
    height, width = image.shape[:2]
    fields_path = Path(args.fields)
    if args.profile:
        candidate = HERE / "profiles" / f"{args.profile.lower()}.yaml"
        if candidate.exists():
            fields_path = candidate
        else:
            print(f"没有 {candidate}，回退到 {fields_path.name}")
    config = load_config(fields_path)
    specs, derived = build_specs(config)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    reader = HudReader(config, specs, derived)
    reader.warmup()

    overlay = image.copy()
    rows: list[dict] = []
    all_ok = True
    print(f"画面 {width}x{height}  档位 {config.get('profile') or '-'}  "
          f"字段 {len(specs)} 个  calibrated={config.get('calibrated')}")
    print(f"{'字段':<12}{'读取':<16}{'置信':<7}{'解析':<10}矩形")
    for spec in specs:
        x, y, w, h = to_pixels(spec.rect, (width, height))
        crop = image[max(0, y):max(0, y + h), max(0, x):max(0, x + w)]
        crop = prepare(crop) if crop.size else crop
        lines = reader.engine.read(crop, mode=spec.mode) if crop.size else []
        threshold = spec.min_score if spec.min_score is not None else reader.min_score
        kept = [line for line in lines if line.score >= threshold]
        text = " ".join(line.text for line in sorted(kept, key=lambda line: line.x))
        score = max((line.score for line in kept), default=0.0)
        ok, value = parse_value(spec, text)
        all_ok = all_ok and ok
        cv2.imwrite(str(out / f"{spec.name}.png"), crop if crop.size else np.zeros((8, 8, 3), np.uint8))
        color = (90, 220, 120) if ok else (80, 80, 240)
        cv2.rectangle(overlay, (x, y), (x + w, y + h), color, 2)
        cv2.putText(overlay, spec.name, (x, max(12, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        shown = json.dumps(value, ensure_ascii=False) if ok else "-"
        print(f"{spec.name:<12}{text[:14]:<16}{score:<7.2f}{shown:<10}"
              f"[{spec.rect[0]:.3f}, {spec.rect[1]:.3f}, {spec.rect[2]:.3f}, {spec.rect[3]:.3f}]"
              f"  {int(w)}x{int(h)}px")
        rows.append({"name": spec.name, "text": text, "score": round(score, 3),
                     "ok": ok, "value": value, "rect": spec.rect,
                     "pixels": [int(x), int(y), int(w), int(h)]})

    for spec in derived:
        print(f"{spec.name:<12}(派生自 {spec.derive})")

    scale = min(1.0, 1600 / max(1, width))
    if scale < 1.0:
        overlay = cv2.resize(overlay, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(out / "overlay.png"), overlay)
    (out / "result.json").write_text(json.dumps(
        {"shot": args.shot, "size": [width, height], "calibrated": config.get("calibrated"),
         "fields": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n裁剪图与 overlay.png 已写入 {out}")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    failed = [row["name"] for row in rows if not row["ok"]]
    if failed:
        print(f"读不出: {', '.join(failed)} -- 改 fields.yaml 里这些字段的 rect 再跑一次")
        return 1
    print("全部字段可读；确认位置后把 fields.yaml 的 calibrated 改成 true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
