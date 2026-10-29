"""Desktop validation build: pull /tick, detect, ask DeepSeek, print the bubble.

This is the "does the whole chain work" harness that runs on the PC before the
APK exists.  It uses the exact payload the phone will send, so the prompt and the
fallbacks can be tuned here and copied over.

    python phone/desktop_runner.py                          # 自动找局域网 PC
    python phone/desktop_runner.py --host 192.168.1.10:8770
    python phone/desktop_runner.py --dry-run                # 只打印送模型的 JSON
    python phone/desktop_runner.py --no-detector            # 跳过 YOLO
    python phone/desktop_runner.py --selftest               # 验证后处理逻辑

Environment:
    DEEPSEEK_API_KEY   missing -> local fallback lines, the loop keeps running

Detector: any Ultralytics YOLOv8 ONNX export at ``phone/models/yolov8n.onnx``
(or ``--model``).  Missing model is not an error -- the chain degrades to
"OCR only", which is what the phone does too.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = Path(__file__).resolve().parent / "models" / "yolov8n.onnx"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_MODEL_FALLBACK = "deepseek-chat"

COCO80 = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

SYSTEM_PROMPT = (
    "你是坐在玩家旁边看比赛的陪玩。语气像熟人，短句，口语，别端着。"
    "只根据给你的 hud 数字和 scene 检测结果说话，不要编造没给你的信息。"
    "每次回 1-2 句，总长不超过 45 个汉字，不要 Markdown，不要表情符号。"
    "玩家没问你就别讲道理，只点评眼前发生的事。"
)


# ------------------------------------------------------------------- transport
def http_json(url: str, payload: dict | None = None, timeout: float = 10.0,
              headers: dict | None = None) -> dict:
    data = None
    request_headers = {"accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["content-type"] = "application/json"
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=request_headers,
                                    method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def pull_tick(base: str, since: int | None, timeout: float = 10.0) -> dict:
    url = f"{base}/tick" + (f"?since={since}" if since is not None else "")
    return http_json(url, timeout=timeout)


# -------------------------------------------------------------------- detector
class Detector:
    """YOLOv8 ONNX via onnxruntime.  No Ultralytics dependency at runtime."""

    def __init__(self, model_path: Path, conf: float = 0.35, iou: float = 0.45,
                 imgsz: int = 640, threads: int = 4) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model_path), options,
                                            providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.shape = self.session.get_inputs()[0].shape
        self.imgsz = int(self.shape[2]) if isinstance(self.shape[2], int) else imgsz
        self.conf = conf
        self.iou = iou
        self.labels = COCO80
        self.name = model_path.name

    @staticmethod
    def letterbox(image: np.ndarray, size: int) -> tuple[np.ndarray, float, tuple[int, int]]:
        height, width = image.shape[:2]
        scale = min(size / width, size / height)
        new_w, new_h = int(round(width * scale)), int(round(height * scale))
        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        pad_x, pad_y = (size - new_w) // 2, (size - new_h) // 2
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
        return canvas, scale, (pad_x, pad_y)

    def detect(self, image: np.ndarray) -> list[dict]:
        canvas, scale, (pad_x, pad_y) = self.letterbox(image, self.imgsz)
        tensor = canvas[:, :, ::-1].astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        output = self.session.run(None, {self.input_name: tensor})[0]
        return self.postprocess(output[0], scale, (pad_x, pad_y), image.shape[:2])

    def postprocess(self, raw: np.ndarray, scale: float, pad: tuple[int, int],
                    original: tuple[int, int]) -> list[dict]:
        """``raw`` is (4 + classes, anchors); Ultralytics emits cx,cy,w,h then scores."""
        if raw.ndim != 2:
            return []
        if raw.shape[0] < raw.shape[1] and raw.shape[0] < 200:  # (84, 8400)
            raw = raw.T
        boxes_raw = raw[:, :4]
        scores_all = raw[:, 4:]
        if scores_all.size == 0:
            return []
        classes = scores_all.argmax(axis=1)
        scores = scores_all[np.arange(len(classes)), classes]
        keep = scores >= self.conf
        if not keep.any():
            return []
        boxes_raw, scores, classes = boxes_raw[keep], scores[keep], classes[keep]
        cx, cy, bw, bh = boxes_raw.T
        x = cx - bw / 2 - pad[0]
        y = cy - bh / 2 - pad[1]
        rects = np.stack([x / scale, y / scale, bw / scale, bh / scale], axis=1)
        indices = cv2.dnn.NMSBoxes(rects.tolist(), scores.tolist(), self.conf, self.iou)
        results = []
        for index in np.array(indices).flatten().tolist():
            px, py, pw, ph = [float(value) for value in rects[index]]
            results.append({
                "cls": self.labels[int(classes[index])]
                if int(classes[index]) < len(self.labels) else str(int(classes[index])),
                "score": round(float(scores[index]), 3),
                "box": [round(px), round(py), round(pw), round(ph)],
                "cx": round((px + pw / 2) / max(1, original[0]), 3),
                "cy": round((py + ph / 2) / max(1, original[1]), 3),
            })
        return results


def summarise(objects: list[dict]) -> list[dict]:
    """Group detections by class and screen third -- the model never sees boxes."""
    if not objects:
        return []
    buckets: dict[tuple[str, str], dict] = {}
    for item in objects:
        region = "left" if item["cx"] < 1 / 3 else ("right" if item["cx"] > 2 / 3 else "center")
        key = (item["cls"], region)
        bucket = buckets.setdefault(key, {"cls": item["cls"], "count": 0, "region": region,
                                          "best": 0.0})
        bucket["count"] += 1
        bucket["best"] = max(bucket["best"], item["score"])
    ordered = sorted(buckets.values(), key=lambda item: (-item["count"], -item["best"]))
    return [{"cls": item["cls"], "count": item["count"], "region": item["region"]}
            for item in ordered[:8]]


# ---------------------------------------------------------------------- events
class EventLog:
    """Turn a stream of field values into the handful of moments worth speaking."""

    TRACKED = ("timer", "allyKills", "enemyKills", "level", "hp", "mana", "gold")

    def __init__(self) -> None:
        self.previous: dict = {}
        self.deaths = 0

    def update(self, values: dict, unstable: list[str]) -> list[dict]:
        events: list[dict] = []
        timer = values.get("timer")
        if isinstance(timer, int) and self.previous.get("timer") not in (None, timer):
            previous = self.previous["timer"]
            if 0 < timer - previous <= 6:
                events.append({"kind": "clock", "value": timer})
        for name in ("allyKills", "enemyKills", "level", "gold"):
            value = values.get(name)
            if isinstance(value, int) and name in self.previous:
                if value != self.previous[name] and name not in unstable:
                    events.append({"kind": f"{name}Change", "from": self.previous[name],
                                   "to": value})
        hp = values.get("hp")
        if isinstance(hp, list) and len(hp) == 2:
            old = self.previous.get("hp")
            if isinstance(old, list) and len(old) == 2 and hp[0] == 0 and old[0] > 0:
                self.deaths += 1
                events.append({"kind": "death", "count": self.deaths})
        for name in self.TRACKED:
            if name in values:
                self.previous[name] = values[name]
        return events

    def unchanged(self, values: dict) -> bool:
        return all(self.previous.get(name) == values.get(name) for name in values) if values else True


# -------------------------------------------------------------------- deepseek
def build_payload(values: dict, unstable: list[str], calibrated: bool,
                  scene: list[dict], events: list[dict], ask: str,
                  game: str = "") -> dict:
    return {
        "game": game,
        "scene": {"objects": scene},
        "hud": {"values": values, "unstable": unstable, "calibrated": calibrated},
        "events": events,
        "ask": ask,
    }


def fallback_line(payload: dict) -> str:
    """No key, no network, model down -- still say something true about the frame."""
    hud = payload.get("hud", {})
    values = hud.get("values", {})
    events = payload.get("events", [])
    for event in events:
        if event.get("kind") == "death":
            return "这波没了，别急，等人齐再打。"
        if event.get("kind") in ("allyKills", "enemyKills"):
            good = event["kind"] == "allyKills" and event.get("to", 0) > event.get("from", 0)
            return "拿下一个，节奏来了。" if good else "对面又拿一个，稳一点。"
    objects = payload.get("scene", {}).get("objects") or []
    if objects:
        top = objects[0]
    else:
        top = None
    timer = values.get("timer")
    clock = f"{timer // 60}:{timer % 60:02d}" if isinstance(timer, int) else None
    if top and top["count"] >= 3:
        return f"{top['region']} 那边人不少，注意点。"
    if clock:
        return f"{clock}，先补刀，别越线。"
    return "画面有点安静，稳住补刀。"


def ask_deepseek(payload: dict, api_key: str, model: str = DEEPSEEK_MODEL,
                 timeout: float = 8.0) -> tuple[str, str]:
    """Return ``(text, source)``; ``source`` is ``deepseek`` or ``fallback``."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0.9,
        "max_tokens": 120,
        "stream": False,
    }
    headers = {"authorization": f"Bearer {api_key}"}
    for attempt in range(2):
        try:
            reply = http_json(DEEPSEEK_URL, body, timeout=timeout, headers=headers)
            text = (reply.get("choices") or [{}])[0].get("message", {}).get("content", "")
            text = clean_reply(text)
            if text:
                return text, "deepseek"
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            if error.code in (400, 422) and "model" in detail.lower() and model != DEEPSEEK_MODEL_FALLBACK:
                model = DEEPSEEK_MODEL_FALLBACK
                body["model"] = model
                continue
            print(f"[deepseek] HTTP {error.code}: {detail}")
        except Exception as error:  # noqa: BLE001
            print(f"[deepseek] {type(error).__name__}: {error}")
        time.sleep(0.4)
    return fallback_line(payload), "fallback"


def clean_reply(text: str) -> str:
    text = re.sub(r"[`*#]+", "", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    parts = [part.strip() for part in re.split(r"(?<=[。！？!?])", text) if part.strip()]
    return " ".join(parts[:3])[:120]


def split_lines(text: str, limit: int = 3) -> list[str]:
    parts = [part.strip() for part in re.split(r"(?<=[。！？!?])", text) if part.strip()]
    if not parts:
        return []
    return parts[:limit]


# ------------------------------------------------------------------------ main
def run_selftest() -> int:
    """Verify the ONNX post-processing without a model file."""
    detector = Detector.__new__(Detector)
    detector.conf, detector.iou, detector.labels, detector.name = 0.35, 0.45, COCO80, "selftest"
    anchors = 8400
    raw = np.zeros((84, anchors), dtype=np.float32)
    # Three boxes: two people (one heavily overlapped duplicate), one car.
    for index, (cx, cy, w, h, cls, score) in enumerate([
        (320, 180, 40, 120, 0, 0.92),
        (322, 182, 40, 120, 0, 0.71),
        (500, 300, 160, 80, 2, 0.66),
    ]):
        raw[0, index], raw[1, index], raw[2, index], raw[3, index] = cx, cy, w, h
        raw[4 + cls, index] = score
    boxes = detector.postprocess(raw, scale=1.0, pad=(0, 0), original=(640, 360))
    scene = summarise(boxes)
    log = EventLog()
    first = log.update({"timer": 100, "allyKills": 3}, [])
    second = log.update({"timer": 106, "allyKills": 4}, [])
    payload = build_payload({"timer": 106, "allyKills": 4}, [], False, scene, second, "test")
    checks = {
        "nms 去掉重复人": sum(1 for box in boxes if box["cls"] == "person") == 1,
        "识别到车": any(box["cls"] == "car" for box in boxes),
        "分区聚合": any(item["cls"] == "person" and item["region"] == "center" for item in scene),
        "首帧无事件": first == [],
        "击杀与时钟事件": {event["kind"] for event in second} == {"clock", "allyKillsChange"},
        "兜底文案是中文": bool(re.search(r"[\u4e00-\u9fff]", fallback_line(payload))),
        "回复清洗": clean_reply("**好**球。\n下一波。") == "好球。 下一波。",
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="桌面验证版：拉 /tick → 检测 → DeepSeek → 打印")
    parser.add_argument("--host", default="127.0.0.1:8770", help="PC 采集的 host:port")
    parser.add_argument("--scheme", default="http")
    parser.add_argument("--interval", type=float, default=2.0, help="两次点评的最小间隔（秒）")
    parser.add_argument("--poll", type=float, default=0.4, help="拉取间隔（秒）")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--no-detector", action="store_true")
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--iou", type=float, default=0.45)
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    parser.add_argument("--deepseek-model", default=DEEPSEEK_MODEL)
    parser.add_argument("--ask", default="看一眼现在该干嘛，一句话。")
    parser.add_argument("--dry-run", action="store_true", help="只打印要送模型的 JSON")
    parser.add_argument("--once", action="store_true", help="只处理一帧就退出")
    parser.add_argument("--duration", type=float, default=0, help="运行秒数，0 表示一直跑")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()

    base = f"{args.scheme}://{args.host}"

    detector = None
    if not args.no_detector:
        model_path = Path(args.model)
        if model_path.exists():
            try:
                detector = Detector(model_path, conf=args.conf, iou=args.iou)
                print(f"[detector] {model_path.name} imgsz={detector.imgsz} "
                      f"conf={args.conf} iou={args.iou}")
            except Exception as error:  # noqa: BLE001
                print(f"[detector] 加载失败，降级为纯 OCR: {type(error).__name__}: {error}")
        else:
            print(f"[detector] 没有模型 {model_path}，降级为纯 OCR（手机端同样行为）")

    if not args.api_key:
        print("[deepseek] 没有 DEEPSEEK_API_KEY，使用本地兜底文案")

    print(f"[pull] {base}/tick  间隔 {args.interval:g}s"
          f"{'  (dry-run)' if args.dry_run else ''}")
    since: int | None = None
    log = EventLog()
    last_spoken = 0.0
    started = time.time()
    skipped = 0

    try:
        while True:
            if args.duration and time.time() - started > args.duration:
                break
            try:
                tick = pull_tick(base, since)
            except Exception as error:  # noqa: BLE001
                print(f"[pull] 连接失败 {type(error).__name__}: {error} -- 1s 后重试")
                time.sleep(1.0)
                since = None
                continue
            if tick.get("unchanged"):
                time.sleep(args.poll)
                continue
            seq = tick.get("seq")
            since = seq if isinstance(seq, int) else None
            ocr = tick.get("ocr") or {}
            values = ocr.get("values") or {}
            unstable = ocr.get("unstable") or []

            objects: list[dict] = []
            if detector and tick.get("image"):
                try:
                    buffer = base64.b64decode(tick["image"])
                    frame = cv2.imdecode(np.frombuffer(buffer, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        objects = detector.detect(frame)
                except Exception as error:  # noqa: BLE001
                    print(f"[detector] 推理失败: {type(error).__name__}: {error}")
            scene = summarise(objects)

            events = log.update(values, unstable)
            payload = build_payload(values, unstable, bool(ocr.get("calibrated")),
                                    scene, events, args.ask, game=tick.get("profile", ""))

            if args.dry_run:
                print(json.dumps(payload, ensure_ascii=False, indent=1))
                if args.once:
                    break
                time.sleep(args.poll)
                continue

            due = events or (time.time() - last_spoken >= args.interval)
            if not values and not scene:
                skipped += 1
                if skipped % 20 == 1:
                    print(f"[tick] seq {seq} 没有可读数字，也没有检测目标"
                          f"（OCR {ocr.get('ms')}ms）")
                time.sleep(args.poll)
                continue
            if not due:
                time.sleep(args.poll)
                continue

            text, source = ask_deepseek(payload, args.api_key, args.deepseek_model) \
                if args.api_key else (fallback_line(payload), "fallback")
            lines = split_lines(text)
            stamp = time.strftime("%H:%M:%S")
            head = " ".join(f"{key}={value}" for key, value in list(values.items())[:4])
            if tick.get("profile"):
                head = f"[{tick['profile']}] {head}"
            print(f"\n[{stamp}] {head}")
            if scene:
                print("          场景 " + ", ".join(
                    f"{item['cls']}x{item['count']}@{item['region']}" for item in scene[:4]))
            for line in lines:
                print(f"          桌宠> {line}")
            print(f"          ({source}, {len(text)} 字)")
            last_spoken = time.time()
            if args.once:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\n[exit] 收到 Ctrl-C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
