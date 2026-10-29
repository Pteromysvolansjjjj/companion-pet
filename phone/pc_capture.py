"""PC side: capture the screen, read the HUD numbers, serve one /tick to the phone.

Three jobs, nothing else -- no model, no window, no AI:

    capture   grab the screen at N FPS, keep one full-resolution frame
    OCR       crop the ROIs from ``phone/fields.yaml``, read them, turn them into
              numbers (this is the only place OCR happens in the whole system)
    serve     GET /tick   frame + numbers in one payload
              GET /frame  raw JPEG, kept for debugging
              GET /info   counters
              GET /apk    newest APK from phone/dist, for one-tap install

    python phone/pc_capture.py                       # 全屏, 5 FPS, 宽 640
    python phone/pc_capture.py --window-title 英雄联盟
    python phone/pc_capture.py --source image --image vision/samples/synthetic-hud.png
    python phone/pc_capture.py --no-ocr              # 只有画面, 跳过 RapidOCR

``--source image`` replays a still image instead of the desktop.  That is how the
whole chain is verified without the game running.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field as dataclass_field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import cv2
import numpy as np
import yaml

# 输出被重定向到文件或管道时用 UTF-8，日志才读得出中文；
# 直接开控制台时保持系统默认编码，避免 Windows 控制台乱码。
if not sys.stdout.isatty():
    for _stream in (sys.stdout, sys.stderr):
        try:
            # line_buffering：重定向到文件时不加这个，横幅会卡在缓冲区里看不到
            _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass


def app_dir() -> Path:
    """配置、模型、APK 所在的目录。

    源码运行时是 ``phone/``；被 PyInstaller 冻结成 exe 后是 **exe 所在目录**
    （``__file__`` 那会儿指向临时解包目录，不能用来找配置）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
ROOT = APP_DIR.parent if not getattr(sys, "frozen", False) else APP_DIR
sys.path.insert(0, str(ROOT / "vision"))

APK_DIR = APP_DIR / "dist"
MODEL_DIR = APP_DIR / "models"
PROFILE_DIR = APP_DIR / "profiles"
SETTINGS_FILE = APP_DIR / "pet.json"

DEFAULT_SETTINGS = {
    "_说明": "改完保存，重新打开 exe 生效。profile 可选 lol / valorant / delta，留空按 lol。",
    "profile": "lol",
    "port": 8770,
    "fps": 5,
    "width": 640,
    "quality": 70,
    "windowTitle": "",
}

# Fallback used when fields.yaml is missing or unreadable.  Kept in sync with
# the four fields that the default HUD really shows.
FALLBACK_CONFIG: dict = {
    "version": 1,
    "calibrated": False,
    "basis": {"width": 1920, "height": 1080},
    "ocr": {"every_ms": 500, "stale_ms": 1500, "min_score": 0.30, "threads": 3},
    "fields": {
        "timer": {"rect": [0.4274, 0.0000, 0.1452, 0.0480], "kind": "clock"},
        "allyKills": {"rect": [0.3350, 0.0048, 0.0660, 0.0384], "kind": "int", "max": 300},
        "enemyKills": {"rect": [0.5990, 0.0048, 0.0660, 0.0384], "kind": "int", "max": 300},
        "level": {"rect": [0.3720, 0.9050, 0.0280, 0.0400], "kind": "int", "min": 1, "max": 18},
        "score": {"derive": ["allyKills", "enemyKills"]},
    },
}

INT_RE = re.compile(r"(?<!\d)(\d{1,4})(?!\d)")
PAIR_RE = re.compile(r"(?<!\d)(\d{1,4})\s*[/／|:：]\s*(\d{1,4})(?!\d)")
CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)")
BARE_CLOCK_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(?!\d)")
FLOAT_RE = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,2})(?![\d.])")


def tighten(image: np.ndarray) -> np.ndarray:
    """Crop a HUD slot down to its ink.

    RapidOCR is trained on tightly framed text.  Handing it a slot with 40 px of
    empty margin on each side is what turns a lone "8" into "¥８" at 0.39
    confidence; trimming first gives "8" at 0.35-0.50 and lifts multi-character
    fields (23:41 went 0.64 -> 0.83).  A busy background just fails the ink test
    and the original crop is returned unchanged.
    """
    if image is None or image.size == 0:
        return image
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    _, mask = cv2.threshold(cv2.GaussianBlur(gray, (3, 3), 0), 0, 255,
                            cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if mask.mean() > 127:  # dark text on a light HUD element
        mask = cv2.bitwise_not(mask)
    ys, xs = np.where(mask > 0)
    if len(xs) < 8:
        return image
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    if (x1 - x0) < 4 or (y1 - y0) < 6:
        return image
    pad_x, pad_y = max(2, int((x1 - x0) * 0.08)), max(2, int((y1 - y0) * 0.10))
    top, bottom = max(0, y0 - pad_y), min(image.shape[0], y1 + 1 + pad_y)
    left, right = max(0, x0 - pad_x), min(image.shape[1], x1 + 1 + pad_x)
    return image[top:bottom, left:right]


def prepare(image: np.ndarray, target_height: int = 56) -> np.ndarray:
    """Tighten, then scale the text up to roughly what the recogniser expects."""
    image = tighten(image)
    if image is None or image.size == 0:
        return image
    height = image.shape[0]
    if height < target_height:
        scale = min(4.0, target_height / max(1, height))
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return image


def lan_ip() -> str:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except OSError:
        return "127.0.0.1"


# --------------------------------------------------------------------- fields
@dataclass
class FieldSpec:
    name: str
    rect: list[float] | None = None
    kind: str = "int"
    mode: str = "line"
    regex: str | None = None
    low: float | None = None
    high: float | None = None
    scale: float = 1.0
    enabled: bool = True
    derive: list[str] | None = None
    purpose: str = ""
    min_score: float | None = None
    crop_tight: bool = True
    enhance: str = ""          # "" | "bright"，先做预处理再送 OCR
    enhance_pct: float = 88.0  # bright 模式保留多亮算「字」
    loose_clock: bool = False  # 计时器冒号丢失时按 M:SS 还原
    pattern: re.Pattern | None = dataclass_field(default=None, repr=False)


def load_config(path: Path) -> dict:
    """Read a config file and overlay it on the fallback.

    Two behaviours, decided by whether the file declares ``profile``:

    * ``phone/fields.yaml`` (no ``profile`` key) — a partial file is allowed, its
      fields are merged over the defaults, so deleting one entry keeps it working.
    * ``phone/profiles/<name>.yaml`` — a complete game profile.  Its ``fields``
      replace the defaults outright; otherwise LoL's timer/kills would be OCR'd
      (and reported) while playing Valorant.
    """
    data: dict = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as error:  # noqa: BLE001 - a broken yaml must not kill capture
            print(f"[warn] {path.name} 解析失败，改用内置默认值: {error}")
    config = json.loads(json.dumps(FALLBACK_CONFIG))
    for key, value in data.items():
        if key != "fields":
            config[key] = value
    if data.get("profile"):
        config["fields"] = dict(data.get("fields") or {})
        return config
    config.setdefault("fields", {})
    for name, entry in (data.get("fields") or {}).items():
        base = config["fields"].get(name, {})
        config["fields"][name] = {**base, **(entry or {})}
    return config


def build_specs(config: dict) -> tuple[list[FieldSpec], list[FieldSpec]]:
    plain: list[FieldSpec] = []
    derived: list[FieldSpec] = []
    for name, entry in (config.get("fields") or {}).items():
        entry = entry or {}
        if entry.get("derive"):
            derived.append(FieldSpec(name=name, derive=list(entry["derive"]),
                                    purpose=entry.get("purpose", "")))
            continue
        spec = FieldSpec(
            name=name,
            rect=list(entry["rect"]) if entry.get("rect") else None,
            kind=str(entry.get("kind") or "int").lower(),
            mode=str(entry.get("mode") or "line").lower(),
            regex=entry.get("regex"),
            low=entry.get("min"),
            high=entry.get("max"),
            scale=float(entry.get("scale") or 1.0),
            enabled=bool(entry.get("enabled", True)),
            purpose=entry.get("purpose", ""),
            min_score=entry.get("min_score"),
            crop_tight=bool(entry.get("crop_tight", True)),
            enhance=str(entry.get("enhance") or "").lower(),
            enhance_pct=float(entry.get("enhancePct") or 88.0),
            loose_clock=bool(entry.get("looseClock", False)),
        )
        if spec.rect and spec.enabled:
            if spec.regex:
                try:
                    spec.pattern = re.compile(spec.regex)
                except re.error as error:
                    print(f"[warn] 字段 {name} 的 regex 无效，改用 kind 解析: {error}")
            plain.append(spec)
    return plain, derived


def _clean(text: str) -> str:
    """Fold full-width digits/punctuation so OCR output parses as ASCII."""
    table = str.maketrans({
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4", "５": "5", "６": "6",
        "７": "7", "８": "8", "９": "9", "：": ":", "／": "/", "．": ".", "－": "-",
    })
    return re.sub(r"\s+", " ", str(text or "").translate(table)).strip()


def enhance_bright(image: np.ndarray, percentile: float = 88.0) -> np.ndarray:
    """把亮字从花背景里抠出来。

    HUD 数字压在建筑/天空上时对比度很低（瓦罗兰特顶部分数只有 0.04 置信度），
    只保留最亮的一成像素后，同样的字能到 0.43。
    """
    if image is None or image.size == 0:
        return image
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    level = float(np.percentile(gray, percentile))
    if level <= 1:
        return image
    _, binary = cv2.threshold(gray, level, 255, cv2.THRESH_BINARY)
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR) if binary.ndim == 2 else binary


def parse_value(spec: FieldSpec, text: str):
    """Return ``(ok, value)`` for one field; ``ok`` false means "not readable"."""
    cleaned = _clean(text)
    if not cleaned:
        return False, None

    value = None
    if spec.pattern is not None:
        match = spec.pattern.search(cleaned)
        if not match:
            return False, None
        try:
            groups = match.groupdict()
            if "a" in groups and "b" in groups:
                value = [int(groups["a"]), int(groups["b"])]
            elif "m" in groups and "s" in groups:
                value = int(groups["m"]) * 60 + int(groups["s"])
            elif "v" in groups:
                value = float(groups["v"])
            else:
                raw = match.group(1) if match.groups() else match.group(0)
                value = float(raw)
        except (TypeError, ValueError):
            # 用户写的可选命名组没匹配上，或组里不是数字
            return False, None
    elif spec.kind == "pair":
        match = PAIR_RE.search(cleaned)
        if match:
            value = [int(match.group(1)), int(match.group(2))]
        else:
            numbers = [int(m.group(1)) for m in INT_RE.finditer(cleaned)][:2]
            value = numbers if len(numbers) == 2 else None
    elif spec.kind == "clock":
        match = CLOCK_RE.search(cleaned)
        if match:
            value = int(match.group(1)) * 60 + int(match.group(2))
        else:
            match = BARE_CLOCK_RE.search(cleaned)
            if match:
                value = int(match.group(1)) * 60 + int(match.group(2))
            elif spec.loose_clock:
                # 「1:34」在小图上常被读成「134」，按 M:SS 还原。
                # 瓦罗兰特回合计时最长 2:59，三位数只可能是 M:SS。
                digits = re.fullmatch(r"(\d)(\d{2})", cleaned)
                value = int(digits.group(1)) * 60 + int(digits.group(2)) if digits else None
            else:
                value = None
    elif spec.kind == "float":
        match = FLOAT_RE.search(cleaned)
        value = float(match.group(1)) if match else None
    elif spec.kind == "text":
        value = cleaned
    else:  # int
        match = INT_RE.search(cleaned)
        value = int(match.group(1)) if match else None

    if value is None:
        return False, None
    if spec.scale != 1.0 and isinstance(value, (int, float)):
        value = value * spec.scale
        if isinstance(value, float) and value.is_integer():
            value = int(value)
    if isinstance(value, (int, float)):
        if spec.low is not None and value < spec.low:
            return False, None
        if spec.high is not None and value > spec.high:
            return False, None
    if isinstance(value, list):
        if spec.high is not None and any(item > spec.high for item in value):
            return False, None
        if len(value) == 2 and value[1] and value[0] > value[1] and spec.kind == "pair":
            # "100/87" is a mis-read of a current/max pair far more often than a real value.
            value = [value[1], value[0]]
    return True, value


class HudReader:
    """OCR thread body: turn one frame into ``{values, unstable, ms}``."""

    def __init__(self, config: dict, specs: list[FieldSpec], derived: list[FieldSpec]) -> None:
        from lolvision.ocr import OcrEngine

        ocr = config.get("ocr") or {}
        self.min_score = float(ocr.get("min_score", 0.30))
        self.stale_ms = float(ocr.get("stale_ms", 1500))
        self.every_ms = float(ocr.get("every_ms", 500))
        self.engine = OcrEngine(min_score=self.min_score, threads=int(ocr.get("threads", 3)))
        self.specs = specs
        self.derived = derived
        self.state: dict[str, dict] = {
            spec.name: {"value": None, "ok": False, "at": 0.0, "text": "", "score": 0.0}
            for spec in specs
        }
        self.last_ms = 0.0
        self.reads = 0
        self.failures = 0

    def warmup(self) -> float:
        try:
            return self.engine.warmup()
        except Exception as error:  # noqa: BLE001
            print(f"[warn] OCR 预热失败: {type(error).__name__}: {error}")
            return 0.0

    def read(self, frame: np.ndarray, origin: tuple[int, int] = (0, 0)) -> dict:
        from lolvision.layout import to_pixels

        height, width = frame.shape[:2]
        started = time.perf_counter()
        fresh: dict[str, dict] = {}
        for spec in self.specs:
            x, y, w, h = to_pixels(spec.rect, (width, height), origin)
            crop = frame[max(0, y):max(0, y + h), max(0, x):max(0, x + w)]
            if crop.size and spec.enhance == "bright":
                crop = enhance_bright(crop, spec.enhance_pct)
            if crop.size and spec.crop_tight:
                crop = prepare(crop)
            lines = self.engine.read(crop, mode=spec.mode) if crop.size else []
            threshold = spec.min_score if spec.min_score is not None else self.min_score
            lines = [line for line in lines if line.score >= threshold]
            text = " ".join(line.text for line in sorted(lines, key=lambda line: line.x))
            score = max((line.score for line in lines), default=0.0)
            ok, value = parse_value(spec, text)
            previous = self.state.get(spec.name) or {}
            if ok:
                fresh[spec.name] = {"value": value, "ok": True, "at": time.time(),
                                    "text": text, "score": round(score, 3)}
            else:
                # 这一帧读不出来：保留上一次的值，但 ok=False、at 不更新，
                # snapshot 会把它放进 unstable，而不是让数字凭空消失。
                fresh[spec.name] = {"value": previous.get("value"), "ok": False,
                                    "at": previous.get("at", 0.0),
                                    "text": text, "score": round(score, 3)}
        self.last_ms = (time.perf_counter() - started) * 1000
        self.reads += 1
        self.state.update(fresh)
        return self.snapshot()

    def snapshot(self) -> dict:
        now = time.time()
        values: dict = {}
        unstable: list[str] = []
        detail: dict = {}
        for spec in self.specs:
            item = self.state.get(spec.name) or {}
            age = (now - item.get("at", 0.0)) * 1000 if item.get("at") else None
            has_value = item.get("ok") and item.get("value") is not None
            if has_value:
                values[spec.name] = item["value"]
                if age is None or age > self.stale_ms:
                    unstable.append(spec.name)
            elif item.get("value") is not None:
                # Reported earlier but not readable now: keep it, flagged.
                values[spec.name] = item["value"]
                unstable.append(spec.name)
            detail[spec.name] = {
                "text": item.get("text", ""),
                "score": item.get("score", 0.0),
                "ageMs": round(age) if age is not None else None,
            }
        for spec in self.derived:
            items = [values.get(name) for name in (spec.derive or [])]
            if all(isinstance(item, int) for item in items):
                values[spec.name] = list(items)
                if any(name in unstable for name in (spec.derive or [])):
                    unstable.append(spec.name)
        if self.reads and not values:
            self.failures += 1
        return {"values": values, "unstable": unstable, "ms": round(self.last_ms, 1),
                "detail": detail}


# -------------------------------------------------------------------- capture
class SayQueue:
    """排队「玩家对陪玩说的话」，手机按 id 增量取走。

    PC 端只负责把文字送到；怎么理解、怎么回，是手机端 DeepSeek 的事。
    这样 PC 端不需要 API Key，也不用知道对话上下文。
    """

    def __init__(self, keep: int = 50) -> None:
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._next = 1
        self._keep = keep
        # 进程级世代号：PC 重启后手机端据此把游标归零，不会漏掉新消息
        self.epoch = int(time.time())

    def push(self, text: str, source: str = "pc") -> dict:
        text = str(text or "").strip()
        if not text:
            raise ValueError("空文本")
        with self._lock:
            item = {"id": self._next, "text": text[:300], "source": source,
                    "at": round(time.time(), 3)}
            self._next += 1
            self._items.append(item)
            if len(self._items) > self._keep:
                self._items = self._items[-self._keep:]
            return item

    def since(self, last_id: int) -> list[dict]:
        with self._lock:
            return [item for item in self._items if item["id"] > last_id]

    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    def latest_id(self) -> int:
        with self._lock:
            return self._next - 1


class Capture:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.width = options.width
        self.quality = options.quality
        self.window_title = (options.window_title or "").lower().strip()
        self.interval = 1.0 / max(0.5, options.fps)
        self.monitor = options.monitor
        self.source = options.source
        self.lock = threading.Lock()
        self.jpeg: bytes = b""
        self.sequence = 0
        self.captured_at = 0.0
        self.capture_ms = 0.0
        self.frames = 0
        self.error: str | None = None
        self.full: np.ndarray | None = None
        self.full_seq = 0
        self.full_size = (0, 0)
        self.sent_size = (0, 0)
        self.ocr_result: dict = {"values": {}, "unstable": [], "ms": 0.0, "detail": {}}
        self.ocr_seq = 0
        self.ocr_at = 0.0
        self.say = SayQueue()
        # 请求计数：手机只有加载了模型才会要「带图」，这两个数字能反证手机端在推理
        self.image_requests = 0
        self.plain_requests = 0
        self._stop = threading.Event()
        self._mss = None
        self._image: np.ndarray | None = None
        if self.source == "image":
            image_path = Path(options.image or "")
            self._image = cv2.imread(str(image_path))
            if self._image is None:
                raise SystemExit(f"读不到图片: {image_path or '(未指定)'}；"
                                 f"--source image 必须配 --image <路径>")

    def _region(self):
        if self.window_title and self._mss is not None:
            try:
                from lolvision.capture import find_windows

                for window in find_windows():
                    if self.window_title in (window.title or "").lower():
                        return {"left": window.x, "top": window.y,
                                "width": window.width, "height": window.height}
            except Exception:
                pass
        bounds = self._mss.monitors[self.monitor]
        return {"left": bounds["left"], "top": bounds["top"],
                "width": bounds["width"], "height": bounds["height"]}

    def _grab(self) -> np.ndarray | None:
        if self._image is not None:
            return self._image
        shot = self._mss.grab(self._region())
        raw = np.frombuffer(shot.bgra, dtype=np.uint8).reshape((shot.height, shot.width, 4))
        return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

    def loop(self) -> None:
        if self.source == "image":
            print(f"[capture] 图片源: {self.options.image} {self._image.shape[1]}x{self._image.shape[0]}")
        else:
            import mss

            self._mss = mss.mss()
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                frame = self._grab()
                if frame is None:
                    raise RuntimeError("grab returned nothing")
                height, width = frame.shape[:2]
                scaled = frame
                if self.width and width != self.width:
                    out_h = max(2, int(round(height * self.width / width)))
                    scaled = cv2.resize(frame, (self.width, out_h), interpolation=cv2.INTER_AREA)
                ok, buffer = cv2.imencode(".jpg", scaled,
                                          [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                if ok:
                    jpeg = buffer.tobytes()
                    with self.lock:
                        self.jpeg = jpeg
                        self.sequence += 1
                        self.captured_at = time.time()
                        self.full = frame
                        self.full_seq = self.sequence
                        self.full_size = (width, height)
                        self.sent_size = (scaled.shape[1], scaled.shape[0])
                        self.frames += 1
                        self.error = None
            except Exception as error:  # noqa: BLE001
                with self.lock:
                    self.error = f"{type(error).__name__}: {error}"
            spent = time.perf_counter() - started
            self.capture_ms = spent * 1000
            time.sleep(max(0.0, self.interval - spent))

    def ocr_loop(self, reader: HudReader) -> None:
        warm = reader.warmup()
        print(f"[ocr] 预热 {warm:.0f} ms，字段 {len(reader.specs)} 个 + 派生 {len(reader.derived)} 个")
        while not self._stop.is_set():
            with self.lock:
                seq, frame, size = self.full_seq, self.full, self.full_size
            if frame is None or seq == self.ocr_seq:
                time.sleep(0.03)
                continue
            gap = reader.every_ms / 1000.0 - (time.perf_counter() - self.ocr_at)
            if gap > 0:
                time.sleep(min(gap, 0.2))
                continue
            try:
                result = reader.read(frame)
            except Exception as error:  # noqa: BLE001 - OCR must never kill capture
                result = {"values": {}, "unstable": [], "ms": 0.0,
                          "detail": {}, "error": f"{type(error).__name__}: {error}"}
            with self.lock:
                self.ocr_result = result
                self.ocr_seq = seq
                self.ocr_at = time.perf_counter()

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------- server
def newest_apk() -> Path | None:
    if not APK_DIR.is_dir():
        return None
    candidates = [path for path in APK_DIR.glob("*.apk") if path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def model_file(name: str) -> Path | None:
    """只允许 ``phone/models`` 下的一层普通文件，挡掉路径穿越与隐藏文件。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidate = MODEL_DIR / name
    return candidate if candidate.is_file() else None


def model_listing(base: str) -> str:
    """手机浏览器打开 /models 时的下载页。模型不入库，靠局域网传到手机上。"""
    if not MODEL_DIR.is_dir():
        items = ""
    else:
        rows = []
        for path in sorted(MODEL_DIR.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix not in (".tflite", ".txt", ".onnx", ".names"):
                continue
            size = path.stat().st_size
            shown = f"{size / 1024 / 1024:.1f} MB" if size > 1024 * 1024 else f"{size} 字节"
            rows.append(
                f'<li><a href="/models/{path.name}">{path.name}</a>'
                f'<span>{shown}</span></li>'
            )
        items = "".join(rows) or "<li>phone/models 下没有可分发的文件</li>"
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>模型下载</title><style>
body{{background:#0b1220;color:#e9f0ff;font-family:system-ui,-apple-system,sans-serif;margin:0;padding:22px}}
h1{{font-size:17px;margin:0 0 6px}}p{{color:#8ea6ff;font-size:12px;line-height:1.6;margin:0 0 16px}}
ul{{list-style:none;padding:0;margin:0}}li{{display:flex;justify-content:space-between;align-items:center;
background:#101a30;border-radius:12px;padding:14px 16px;margin-bottom:10px;font-size:14px}}
a{{color:#7fa0ff;text-decoration:none;word-break:break-all}}span{{color:#7d8cab;font-size:12px;white-space:nowrap;margin-left:12px}}
</style></head><body>
<h1>模型文件</h1>
<p>下载后会落在手机的 Download 目录。<br>
然后在 App 里：设置 → 换模型 → 从文件选择，逐个导入。<br>
当前地址：{base}</p>
<ul>{items}</ul></body></html>"""


def console_say_reader(say_queue: SayQueue, stop: threading.Event) -> None:
    """在这个窗口打字回车 -> 文字送到手机，PC 麦克风之外的第二条路。"""
    try:
        for line in sys.stdin:
            if stop.is_set():
                break
            text = line.strip()
            if not text:
                continue
            if text in (":q", ":quit", ":exit"):
                break
            try:
                item = say_queue.push(text, "typed")
            except ValueError:
                continue
            print(f"  >> 已送到手机 #{item['id']}：{text}", flush=True)
    except Exception:
        pass


def _int_arg(raw, default: int = 0) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def make_handler(capture: Capture, config: dict):
    ocr_enabled = bool(config.get("_ocr_enabled"))
    calibrated = bool(config.get("calibrated"))
    profile = str(config.get("profile") or config.get("_profile") or "lol").lower()

    class Handler(BaseHTTPRequestHandler):
        server_version = "lolcompanion-capture/2"
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            return

        def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.send_header("access-control-allow-origin", "*")
            self.send_header("cache-control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, payload: dict, extra: dict | None = None):
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/say":
                self._json(404, {"error": "not found", "paths": ["/say"]})
                return
            length = _int_arg(self.headers.get("content-length") or 0, 0)
            raw = self.rfile.read(length) if length > 0 else b""
            text = ""
            source = "pc"
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                    text = str(data.get("text") or "")
                    source = str(data.get("source") or "pc")
                except (ValueError, UnicodeDecodeError):
                    text = raw.decode("utf-8", "replace")
            else:
                query = parse_qs(parsed.query)
                text = query.get("text", [""])[0]
                source = query.get("source", ["pc"])[0]
            try:
                item = capture.say.push(text, source)
            except ValueError:
                self._json(400, {"error": "缺少 text"})
                return
            self._json(200, {"ok": True, "id": item["id"], "text": item["text"]})

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == "/tick":
                self._tick(query)
                return
            if path == "/say":
                last = _int_arg(query.get("since", ["0"])[0], 0)
                self._json(200, {"say": capture.say.since(last),
                                 "next": capture.say.latest_id(),
                                 "epoch": capture.say.epoch})
                return
            if path == "/frame":
                with capture.lock:
                    jpeg, seq, at = capture.jpeg, capture.sequence, capture.captured_at
                if not jpeg:
                    self._json(503, {"error": "no frame yet"})
                    return
                self._send(200, jpeg, "image/jpeg",
                           {"x-seq": str(seq), "x-time": f"{at:.3f}"})
                return
            if path == "/info":
                with capture.lock:
                    payload = {
                        "frames": capture.frames,
                        "sequence": capture.sequence,
                        "jpegBytes": len(capture.jpeg),
                        "captureMs": round(capture.capture_ms, 1),
                        "fps": round(1.0 / capture.interval, 1),
                        "width": capture.width,
                        "sourceSize": list(capture.full_size),
                        "source": capture.source,
                        "profile": profile,
                        "error": capture.error,
                        "ocr": {"enabled": ocr_enabled, "lastSeq": capture.ocr_seq,
                                "ms": capture.ocr_result.get("ms"),
                                "values": capture.ocr_result.get("values"),
                                "unstable": capture.ocr_result.get("unstable"),
                                "error": capture.ocr_result.get("error")},
                        "apk": (newest_apk().name if newest_apk() else None),
                        "say": {"pending": capture.say.pending(),
                                "lastId": capture.say.latest_id()},
                        "requests": {"image": capture.image_requests,
                                     "plain": capture.plain_requests},
                    }
                self._json(200, payload)
                return
            if path == "/fields":
                self._json(200, {"profile": profile,
                                 "calibrated": calibrated,
                                 "fields": config.get("fields", {}),
                                 "ocr": config.get("ocr", {})})
                return
            if path == "/apk":
                apk = newest_apk()
                if apk is None:
                    self._json(404, {"error": "phone/dist 下没有 apk",
                                     "hint": "把 assembleDebug 的产物拷到 phone/dist/"})
                    return
                body = apk.read_bytes()
                self._send(200, body, "application/vnd.android.package-archive",
                           {"content-disposition": f'attachment; filename="{apk.name}"'})
                return
            if path == "/models":
                host = self.headers.get("host") or f"{lan_ip()}:{capture.options.port}"
                body = model_listing(f"http://{host}/models").encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return
            if path.startswith("/models/"):
                target = model_file(unquote(path[len("/models/"):]))
                if target is None:
                    self._json(404, {"error": "没有这个文件", "hint": f"打开 /models 看清单"})
                    return
                body = target.read_bytes()
                self._send(200, body, "application/octet-stream",
                           {"content-disposition": f'attachment; filename="{target.name}"'})
                return
            if path == "/apk/info":
                apk = newest_apk()
                if apk is None:
                    self._json(404, {"error": "no apk"})
                    return
                stat = apk.stat()
                self._json(200, {"name": apk.name, "bytes": stat.st_size,
                                 "mtime": int(stat.st_mtime)})
                return
            self._json(404, {"error": "not found", "paths": ["/tick", "/frame", "/info",
                                                            "/fields", "/apk", "/apk/info",
                                                            "/models", "/say"]})

        def _tick(self, query: dict) -> None:
            since = query.get("since", [None])[0]
            want_image = query.get("image", ["1"])[0] not in ("0", "false", "no")
            last_say = _int_arg(query.get("say", ["0"])[0], 0)
            say_items = capture.say.since(last_say)
            if want_image:
                capture.image_requests += 1
            else:
                capture.plain_requests += 1
            with capture.lock:
                seq = capture.sequence
                jpeg, at, size = capture.jpeg, capture.captured_at, capture.sent_size
                ocr = capture.ocr_result
                ocr_at = capture.ocr_at
            if since is not None:
                try:
                    if int(since) == seq:
                        self._json(200, {"unchanged": True, "seq": seq,
                                         "say": say_items, "sayEpoch": capture.say.epoch})
                        return
                except ValueError:
                    pass
            if not jpeg:
                self._json(503, {"error": "no frame yet"})
                return
            payload = {
                "seq": seq,
                "ts": round(at, 3),
                "profile": profile,
                "size": list(size),
                "say": say_items,
                "sayEpoch": capture.say.epoch,
                "ocr": {
                    "values": ocr.get("values") or {},
                    "unstable": ocr.get("unstable") or [],
                    "calibrated": calibrated,
                    "ms": ocr.get("ms") or 0.0,
                    "ageMs": round((time.perf_counter() - ocr_at) * 1000) if ocr_at else None,
                },
            }
            if want_image:
                payload["image"] = base64.b64encode(jpeg).decode("ascii")
            if query.get("debug", ["0"])[0] in ("1", "true", "yes"):
                payload["ocr"]["detail"] = ocr.get("detail") or {}
            self._json(200, payload)

    return Handler


def load_settings() -> dict:
    """读 exe 旁边的 pet.json。

    只对冻结后的 exe 生效：源码运行时保持纯命令行行为，否则本机这份配置会把
    自检和 `--fields` 的预期全带偏（踩过一次：pet.json 里 profile=valorant，
    导致自检里 LoL 字段的 5 项全挂）。
    """
    if not getattr(sys, "frozen", False):
        return {}
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            user = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) or {}
            data.update({key: value for key, value in user.items()
                         if key in DEFAULT_SETTINGS and not key.startswith("_")})
        except Exception as error:  # noqa: BLE001
            print(f"[warn] pet.json 解析失败，用默认值: {error}")
    else:
        try:
            SETTINGS_FILE.write_text(
                json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[init] 已生成 {SETTINGS_FILE.name}，改 profile 可切游戏档位")
        except OSError:
            pass
    return data


def main() -> int:
    settings = load_settings()

    def setting(key: str):
        return settings.get(key, DEFAULT_SETTINGS[key])

    parser = argparse.ArgumentParser(description="PC 采集 + HUD OCR + /tick 服务")
    parser.add_argument("--port", type=int, default=int(setting("port")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--fps", type=float, default=float(setting("fps")))
    parser.add_argument("--width", type=int, default=int(setting("width")),
                        help="下发画面的宽度，0 表示原始尺寸")
    parser.add_argument("--quality", type=int, default=int(setting("quality")))
    parser.add_argument("--monitor", type=int, default=1)
    parser.add_argument("--window-title", default=str(setting("windowTitle")),
                        help="只截标题包含该关键字的窗口")
    parser.add_argument("--source", choices=["screen", "image"], default="screen")
    parser.add_argument("--image", default="",
                        help="配合 --source image 使用的静态图路径（仅自测用）")
    parser.add_argument("--fields", default=str(APP_DIR / "fields.yaml"))
    parser.add_argument("--profile", default=str(setting("profile") or ""),
                        help="游戏档位名，读取 profiles/<name>.yaml；省略则用 --fields")
    parser.add_argument("--no-ocr", action="store_true", help="只发画面，不跑 OCR")
    args = parser.parse_args()

    fields_path = Path(args.fields)
    if args.profile:
        candidate = PROFILE_DIR / f"{args.profile.lower()}.yaml"
        if candidate.exists():
            fields_path = candidate
        elif args.profile.lower() not in ("lol",):
            # lol 的配置本来就是 fields.yaml，不该按「回退」报警
            print(f"[warn] 没有 {candidate.name}，回退到 {fields_path.name}")
    config = load_config(fields_path)
    config["_profile"] = args.profile.lower() if args.profile else ""
    config["_ocr_enabled"] = not args.no_ocr
    specs, derived = build_specs(config)

    capture = Capture(args)
    reader = None
    threads = [threading.Thread(target=capture.loop, name="capture", daemon=True)]
    if not args.no_ocr:
        reader = HudReader(config, specs, derived)
        threads.append(threading.Thread(target=capture.ocr_loop, args=(reader,),
                                        name="ocr", daemon=True))
    for thread in threads:
        thread.start()
    time.sleep(0.8)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(capture, config))
    base = f"http://{lan_ip()}:{args.port}"
    print("=" * 58)
    print("  陪玩桌宠 · PC 采集服务已启动")
    print("=" * 58)
    print(f"  手机填这个地址：{lan_ip()}:{args.port}")
    print(f"  /tick    {base}/tick      <- 手机自动拉")
    print(f"  /models  {base}/models    <- 手机下模型")
    print(f"  /apk     {base}/apk       <- 手机下 APK")
    print(f"  /info    {base}/info")
    print("-" * 58)
    print(f"  画面 {args.width or '原生'} 宽, {args.fps:g} FPS, JPEG q{args.quality}, 源 {args.source}")
    print(f"  档位 {str(config.get('profile') or args.profile or 'lol').lower()}"
          f"  字段 {[spec.name for spec in specs]} 派生 {[spec.name for spec in derived]}"
          f" calibrated={bool(config.get('calibrated'))}")
    apk = newest_apk()
    print(f"  APK {apk.name} ({apk.stat().st_size / 1024 / 1024:.1f} MB)" if apk
          else "  APK 尚未生成（dist 目录为空）")
    print(f"  说话  在这个窗口打字回车 -> 手机的陪玩会回应")
    print(f"        麦克风说话：另开一个窗口跑 phone\\pc_voice.py（按住 {settings.get('voiceHotkey', 'F2')}）")
    print(f"  配置 {SETTINGS_FILE.name}  按 Ctrl-C 停止")
    print("=" * 58)

    say_stop = threading.Event()
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            threading.Thread(target=console_say_reader, args=(capture.say, say_stop),
                             name="console-say", daemon=True).start()
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl-C，正在停止…")
    finally:
        say_stop.set()
        capture.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - 双击启动时窗口会一闪而过，必须停住给人看
        import traceback

        traceback.print_exc()
        exit_code = 1
    if exit_code and getattr(sys, "frozen", False):
        try:
            input("\n出错了，按回车关闭窗口…")
        except EOFError:
            pass
    raise SystemExit(exit_code)
