"""离线分析：游戏结束后，用录下来的帧跑 YOLO 模拟。

这一步**只在游戏关掉之后跑**，游戏进行中绝不碰。
模拟的是手机端真实看到的画面：录制下来的就是 PC 下发的 640 宽 JPEG，
按 Android 端同样的方式 letterbox 成 640x640 再推理。

用法::

    phone\\train\\.venv\\Scripts\\python.exe logs/analyze_live.py --folder logs/live/frames
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

NAMES = ["dropped spike", "enemy", "planted spike", "teammate"]
ROOT = Path(__file__).resolve().parent.parent
ONNX = ROOT / "phone" / "models" / "valorant-yolov8s.onnx"


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def letterbox(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    scale = min(640 / w, 640 / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    canvas = np.full((640, 640, 3), 114, np.uint8)
    top, left = (640 - nh) // 2, (640 - nw) // 2
    canvas[top:top + nh, left:left + nw] = cv2.resize(image, (nw, nh))
    return canvas


class Yolo:
    def __init__(self, path: Path) -> None:
        import onnxruntime as ort

        self.session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.in_name = self.session.get_inputs()[0].name
        self.out_name = self.session.get_outputs()[0].name
        shape = self.session.get_inputs()[0].shape
        self.nchw = len(shape) == 4 and shape[1] == 3

    def detect(self, image: np.ndarray, threshold: float):
        canvas = letterbox(image)
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None]) if self.nchw \
            else np.ascontiguousarray(rgb[None])
        out = np.asarray(self.session.run([self.out_name], {self.in_name: blob})[0])
        if out.ndim == 3:
            out = out[0]
        if out.shape[0] < out.shape[1]:
            out = out.T                      # (anchors, 4+classes)
        scores = out[:, 4:]
        cls = scores.argmax(axis=1)
        conf = scores.max(axis=1)
        keep = np.where(conf > threshold)[0]
        cands = []
        for index in keep:
            row = out[index]
            cx, cy, bw, bh = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            cands.append((float(conf[index]), int(cls[index]),
                          [cx - bw / 2, cy - bh / 2, bw, bh]))
        cands.sort(reverse=True)
        chosen = []
        for c, k, box in cands:
            if all(iou(box, other) <= 0.45 for _, _, other in chosen):
                chosen.append((c, k, box))
        return chosen, float(conf.max()) if conf.size else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="离线 YOLO 模拟（游戏结束后跑）")
    parser.add_argument("--folder", default=str(ROOT / "logs" / "live" / "frames"))
    parser.add_argument("--prefix", default="sent", help="分析 sent（手机收到的）或 full（原图）")
    parser.add_argument("--thresholds", default="0.15,0.20,0.25,0.30")
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=0, help="最多分析多少张，0=全部")
    args = parser.parse_args()

    folder = Path(args.folder)
    files = sorted(folder.glob(f"{args.prefix}-*.jpg"))
    if not files:
        print(f"{folder} 里没有 {args.prefix}-*.jpg")
        return 2
    if args.limit:
        step = max(1, len(files) // args.limit)
        files = files[::step][:args.limit]
    thresholds = [float(x) for x in args.thresholds.split(",")]
    print(f"分析 {len(files)} 张 {args.prefix}-*.jpg（{folder}）")

    yolo = Yolo(ONNX)
    stats = {t: Counter() for t in thresholds}
    best_scores = []
    per_frame = []
    for path in files:
        image = cv2.imread(str(path))
        if image is None:
            continue
        chosen, best = yolo.detect(image, min(thresholds))
        best_scores.append(best)
        entry = {"file": path.name, "best": round(best, 3)}
        for threshold in thresholds:
            counts = Counter()
            for c, k, _ in chosen:
                if c > threshold:
                    counts[NAMES[k] if k < len(NAMES) else str(k)] += 1
            stats[threshold].update(counts)
            entry[f"@{threshold}"] = dict(counts)
        per_frame.append(entry)

    print(f"\n最高分：最大 {max(best_scores):.3f}  中位 {float(np.median(best_scores)):.3f}  "
          f"最小 {min(best_scores):.3f}")
    print("\n各阈值下的累计检出：")
    for threshold in thresholds:
        print(f"  >{threshold:<5} {dict(stats[threshold]) or '无'}")

    hits = sum(1 for s in best_scores if s > min(thresholds))
    print(f"\n有检出的帧：{hits}/{len(best_scores)} "
          f"({hits * 100 // max(1, len(best_scores))}%)")
    if args.out:
        Path(args.out).write_text(json.dumps(per_frame, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print("明细 ->", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
