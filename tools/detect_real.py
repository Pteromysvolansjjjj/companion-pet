"""用真实对局截图跑 valorant.tflite，看检出什么（带 NMS）。"""

import sys

import cv2
import numpy as np
import tensorflow as tf

NAMES = ["dropped spike", "enemy", "planted spike", "teammate"]
MODEL = "phone/models/valorant.tflite"
CONFIDENCE = 0.25


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def detect(interp, image):
    h, w = image.shape[:2]
    scale = min(640 / w, 640 / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    canvas = np.full((640, 640, 3), 114, np.uint8)
    top, left = (640 - nh) // 2, (640 - nw) // 2
    canvas[top:top + nh, left:left + nw] = cv2.resize(image, (nw, nh))
    x = np.ascontiguousarray(
        cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32)[None] / 255.0)
    interp.set_tensor(ind["index"], x)
    interp.invoke()
    out = interp.get_tensor(outd["index"])[0]
    scores = out[4:]
    cls = scores.argmax(axis=0)
    conf = scores.max(axis=0)
    keep = np.where(conf > CONFIDENCE)[0]
    cands = []
    for index in keep:
        cx, cy, bw, bh = out[0][index], out[1][index], out[2][index], out[3][index]
        cands.append((float(conf[index]), int(cls[index]),
                      [float(cx - bw / 2), float(cy - bh / 2), float(bw), float(bh)]))
    cands.sort(reverse=True)
    chosen = []
    for score, cls_id, box in cands:
        if all(iou(box, other) <= 0.45 for _, _, other in chosen):
            chosen.append((score, cls_id, box))
    return chosen, float(conf.max()) if conf.size else 0.0


interp = tf.lite.Interpreter(model_path=MODEL)
interp.allocate_tensors()
ind = interp.get_input_details()[0]
outd = interp.get_output_details()[0]
print(f"模型 {MODEL}  输入 {ind['shape']} {ind['dtype'].__name__}  输出 {outd['shape']}")

for path in sys.argv[1:]:
    image = cv2.imread(path)
    if image is None:
        print(f"读不到 {path}")
        continue
    chosen, best = detect(interp, image)
    counts = {}
    for conf, cls_id, _ in chosen:
        counts[NAMES[cls_id]] = counts.get(NAMES[cls_id], 0) + 1
    print(f"\n== {path.split(chr(92))[-1]}  {image.shape[1]}x{image.shape[0]} ==")
    print(f"   最高分 {best:.3f}   检出 {counts or '无'}")
    for conf, cls_id, box in chosen[:8]:
        print(f"     {NAMES[cls_id]:14} {conf:.2f}  框={[round(v) for v in box]}")
    vis = image.copy()
    for conf, cls_id, box in chosen:
        x, y, w, h = [int(v) for v in box]
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.putText(vis, f"{NAMES[cls_id]} {conf:.2f}", (x, max(16, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    out_path = path.replace(".jpg", "-detect.jpg").replace(".png", "-detect.png")
    cv2.imwrite(out_path, vis)
    print("   可视化:", out_path)
