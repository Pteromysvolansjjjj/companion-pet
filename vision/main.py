"""Vision sidecar entry point.

Protocol: UTF-8 JSON Lines on stdout, one object per line.  Everything else
(library noise, tracebacks) is redirected to stderr so the pipe stays parseable.

Commands in (stdin)
    {"type":"ocr","enabled":true}
    {"type":"voice","enabled":true,"config":{...}}
    {"type":"config","config":{...}}
    {"type":"snapshot"}            one frame now, even if the loop is idle
    {"type":"shutdown"}

Events out (stdout)
    {"type":"ready", ...}
    {"type":"frame", ...}
    {"type":"voice", ...} / {"type":"voice:state", ...}
    {"type":"status", ...}
    {"type":"error", ...}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Keep stdout for the protocol only.  When another module imports this file to
# reuse the pipeline (vision/inspect.py), the redirect is skipped.
if os.environ.get("LOLVISION_NO_REDIRECT") == "1":
    _protocol = sys.stdout
else:
    _protocol = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

import numpy as np  # noqa: E402

from lolvision import __version__, layout as layout_mod, parse  # noqa: E402
from lolvision.capture import Grabber, enable_dpi_awareness, find_windows, select_window  # noqa: E402
from lolvision.feed import EventTracker  # noqa: E402
from lolvision.ocr import OcrEngine  # noqa: E402

WRITE_LOCK = threading.Lock()


def emit(payload: dict) -> None:
    with WRITE_LOCK:
        try:
            _protocol.write(json.dumps(payload, ensure_ascii=False) + "\n")
            _protocol.flush()
        except Exception:
            pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Vision:
    def __init__(self, root: Path, config: dict) -> None:
        self.root = root
        self.config = config
        self.layout = layout_mod.load_layout(root)
        if config.get("layoutPath"):
            override = json.loads(Path(config["layoutPath"]).read_text(encoding="utf-8"))
            self.layout = layout_mod.merge_layout(self.layout, override)
        self.image_path = str(config.get("imagePath") or "")
        self.ocr = OcrEngine(min_score=float(config.get("minScore", 0.45)))
        self.grabber = None if self.image_path else Grabber()
        self.tracker = EventTracker()
        self.sequence = 0
        self.enabled = bool(config.get("ocrEnabled", True))
        self.interval_ms = 1000.0
        self.last_frame_ms = 0.0
        self.last_runs: dict[str, float] = {}
        self.last_window_key: object = None
        self._stop = threading.Event()
        self._snapshot_request = threading.Event()

    # ------------------------------------------------------------------ loop
    def loop(self, interval_ms: int) -> None:
        self.interval_ms = float(interval_ms)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                if self.enabled or self._snapshot_request.is_set():
                    force = self._snapshot_request.is_set()
                    frame = self.capture_frame(force=force)
                    if frame and (self.enabled or force):
                        emit(frame)
                    self._snapshot_request.clear()
            except Exception as error:  # never kill the sidecar on one bad frame
                emit({"type": "error", "scope": "ocr", "code": "capture_failed", "message": str(error)})
            spent = (time.monotonic() - started) * 1000
            time.sleep(max(0.02, (interval_ms - spent) / 1000))

    def stop(self) -> None:
        self._stop.set()

    # --------------------------------------------------------------- capture
    def capture_frame(self, force: bool = False) -> dict | None:
        if self.image_path:
            import cv2

            target = Path(self.image_path)
            if target.is_dir():
                options = sorted(target.glob("*.png")) or sorted(target.glob("*.jpg"))
                if not options:
                    emit({"type": "error", "scope": "ocr", "code": "image_unreadable",
                          "message": f"目录中没有图像：{self.image_path}"})
                    self._stop.set()
                    return None
                target = options[min(self.sequence, len(options) - 1)]
            still = cv2.imread(str(target))
            if still is None:
                emit({"type": "error", "scope": "ocr", "code": "image_unreadable",
                      "message": f"无法读取图像：{target}"})
                self._stop.set()
                return None
            meta = {"title": target.name, "width": still.shape[1],
                    "height": still.shape[0], "foreground": True, "kind": "image"}
            return self._process(still, meta, force)

        window = select_window(prefer_foreground=bool(self.config.get("requireForeground", True)))
        if window is None:
            if self.last_window_key != "none":
                self.last_window_key = "none"
                emit({"type": "status", "window": None, "windows": [w.as_dict() for w in find_windows()]})
            return None

        if (window.width, window.height) != self.last_window_key:
            self.last_window_key = (window.width, window.height)
            emit({"type": "status", "window": window.as_dict()})

        frame_bgr = self.grabber.grab(window.x, window.y, window.width, window.height)
        return self._process(frame_bgr, window.as_dict(), force)

    # --------------------------------------------------------------- process
    def _process(self, frame_bgr, window: dict, force: bool) -> dict:
        started = time.perf_counter()
        # Detection is expensive; only spend it when the previous frame left
        # enough headroom inside the sampling interval.
        det_allowed = force or self.last_frame_ms <= max(350.0, self.interval_ms * 0.6)
        size = (int(window["width"]), int(window["height"]))
        regions = self.layout.get("regions", {})
        fields_cfg = self.layout.get("fields", {})
        lines: list[dict] = []
        region_ms: dict[str, float] = {}

        def crop(rect):
            x, y, w, h = layout_mod.to_pixels(rect, size)
            x = max(0, min(size[0] - 1, x))
            y = max(0, min(size[1] - 1, y))
            w = max(1, min(size[0] - x, w))
            h = max(1, min(size[1] - y, h))
            return frame_bgr[y:y + h, x:x + w]

        def timed(name: str, fn):
            tick = time.perf_counter()
            value = fn()
            region_ms[name] = round((time.perf_counter() - tick) * 1000, 1)
            return value

        # Scoreboard first: when it is open it covers the kill feed area.
        board_result = None
        board_cfg = regions.get("board")
        board_lines_read: list[dict] = []
        if board_cfg and board_cfg.get("enabled", True) and (force or self._due("board", float(board_cfg.get("everyMs", 1800)))):
            if str(board_cfg.get("mode")) == "det" and det_allowed:
                raw = timed("board", lambda: self.ocr.read(crop(board_cfg["rect"]), mode="det"))
                board_result = parse.parse_scoreboard(raw)
                board_lines_read = [{"text": line.text, "score": round(line.score, 3), "region": "board"} for line in raw]
            elif str(board_cfg.get("mode")) != "det":
                buffered: list[dict] = []
                texts = timed("board", lambda: self._read_slots(crop, board_cfg, buffered, "board"))
                board_result = parse.parse_board_texts(texts)
                board_lines_read = buffered
        board_visible = bool(board_result and self._looks_like_board(board_result))
        # Text read while Tab is closed is just the game scene; never pass it on
        # as if it were part of the scoreboard.
        if board_visible:
            lines.extend(board_lines_read)

        # Kill feed
        feed_cfg = regions.get("feed")
        feed_texts: list[str] = []
        if feed_cfg and (force or self._due("feed", float(feed_cfg.get("everyMs", 900)))):
            feed_texts = timed("feed", lambda: self._read_slots(crop, feed_cfg, lines, "feed"))

        # Top bar: team kills + clock
        top_cfg = regions.get("top")
        top_fields: dict = {}
        if top_cfg and (force or self._due("top", float(top_cfg.get("everyMs", 700)))):
            top_fields = timed("top", lambda: self._read_top(crop, top_cfg, lines))

        # Centre announcements
        center_cfg = regions.get("center")
        if center_cfg and det_allowed and (force or self._due("center", float(center_cfg.get("everyMs", 3000)))):
            timed("center", lambda: self._read_det(crop(center_cfg["rect"]), lines, "center"))

        # Own HUD (optional)
        hud_cfg = regions.get("hud")
        if hud_cfg and (force or self._due("hud", float(hud_cfg.get("everyMs", 3000)))):
            if str(hud_cfg.get("mode")) == "det":
                if det_allowed:
                    timed("hud", lambda: self._read_det(crop(hud_cfg["rect"]), lines, "hud"))
            else:
                timed("hud", lambda: self._read_slots(crop, hud_cfg, lines, "hud"))

        # Explicit numeric fields
        fields: dict = {}
        for name, spec in fields_cfg.items():
            if not (force or self._due("field:" + name, float(spec.get("everyMs", 1000)))):
                continue
            text = self._read_line(crop(spec["rect"]), None, None)
            if not text:
                continue
            if name == "gameTime":
                seconds = parse.parse_clock(text)
                if seconds is not None:
                    fields["gameTime"] = seconds
            elif name == "level":
                level = parse.parse_level(text)
                if level is not None:
                    fields["level"] = level

        if "gameTime" not in fields and "gameTime" in top_fields:
            fields["gameTime"] = top_fields["gameTime"]
        for key in ("allyKills", "enemyKills"):
            if key in top_fields and key not in fields:
                fields[key] = top_fields[key]

        # Events never come from the scoreboard: it repeats kills from the whole game.
        events = [] if board_visible else self.tracker.update(feed_texts, fields.get("gameTime"))
        bars = self._probe_bars(crop)

        self.sequence += 1
        frame = {
            "type": "frame",
            "id": self.sequence,
            "capturedAt": now_iso(),
            "window": window,
            "durationMs": round((time.perf_counter() - started) * 1000, 1),
            "regions": region_ms,
            "fields": fields,
            "bars": bars,
            "board": board_result if board_visible else None,
            "lines": lines,
            "events": events,
        }
        self.last_frame_ms = frame["durationMs"]
        return frame

    # -------------------------------------------------------------- helpers
    def _due(self, name: str, every_ms: float) -> bool:
        now = time.monotonic() * 1000
        if now - self.last_runs.get(name, 0) >= every_ms:
            self.last_runs[name] = now
            return True
        return False

    def _read_line(self, image, lines, region) -> str:
        result = self.ocr.read(image, mode="line")
        text = " ".join(item.text for item in result if item.score >= 0.25).strip()
        if text and lines is not None:
            lines.append({"text": text, "score": round(max(i.score for i in result), 3), "region": region})
        return text

    def _read_slots(self, crop, region: dict, lines: list[dict], name: str = "feed") -> list[str]:
        texts: list[str] = []
        for slot in layout_mod.slot_rects(region):
            result = self.ocr.read(crop(slot), mode="line")
            text = " ".join(item.text for item in result if item.score >= 0.5).strip()
            if text:
                texts.append(text)
                lines.append({"text": text, "score": round(max(i.score for i in result), 3), "region": name})
        return texts

    def _read_top(self, crop, region: dict, lines: list[dict]) -> dict:
        parts = region.get("parts")
        if not parts:
            text = self._read_line(crop(region["rect"]), lines, "top")
            return parse.parse_top_bar(text) if text else {}
        values: dict = {}
        texts: dict[str, str] = {}
        for name, part in parts.items():
            result = self.ocr.read(crop(layout_mod.part_rect(region, part)), mode="line")
            text = " ".join(item.text for item in result if item.score >= 0.25).strip()
            if not text:
                continue
            texts[name] = text
            lines.append({"text": text, "score": round(max(i.score for i in result), 3), "region": "top"})
        if "clock" in texts:
            seconds = parse.parse_clock(texts["clock"])
            if seconds is not None:
                values["gameTime"] = seconds
        for side, key in (("ally", "allyKills"), ("enemy", "enemyKills")):
            if side in texts:
                numbers = [int(m.group(1)) for m in parse.INT_RE.finditer(parse.normalise(texts[side]))]
                if numbers:
                    values[key] = numbers[0] % 1000
        if not values and texts:
            return parse.parse_top_bar(" ".join(texts.values()))
        return values

    def _read_det(self, image, lines: list[dict], region: str) -> list:
        result = self.ocr.read(image, mode="det")
        for item in result:
            if item.score >= 0.5:
                lines.append({"text": item.text, "score": round(item.score, 3), "region": region})
        return result

    @staticmethod
    def _looks_like_board(board: dict) -> bool:
        rows = board.get("rows") or []
        with_kda = [row for row in rows if row.get("kills") is not None]
        return len(with_kda) >= 4 and len(rows) >= 6

    def _probe_bars(self, crop) -> dict:
        result: dict = {}
        for name, spec in (self.layout.get("bars") or {}).items():
            if not spec.get("enabled"):
                continue
            try:
                ratio, confidence = bar_ratio(crop(spec["rect"]), name)
                if confidence >= 0.35:
                    result[name] = round(ratio, 3)
            except Exception:
                continue
        return result


def bar_ratio(image, kind: str) -> tuple[float, float]:
    """Fraction of the probe rectangle filled with health/mana colour."""
    import cv2

    if image is None or image.size == 0:
        return 0.0, 0.0
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    if kind == "hp":
        lower, upper = np.array([35, 80, 80]), np.array([95, 255, 255])
    else:
        lower, upper = np.array([95, 90, 90]), np.array([135, 255, 255])
    mask = cv2.inRange(hsv, lower, upper)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    counts = mask.sum(axis=0) / 255.0
    filled = counts > (mask.shape[0] * 0.5)
    if not filled.any():
        return 0.0, 0.0
    best = current = 0
    for value in filled:
        current = current + 1 if value else 0
        best = max(best, current)
    return min(1.0, best / max(1, len(filled))), float(filled.mean())


def read_commands(state: dict) -> None:
    from lolvision.voice import VoiceConfig, VoiceService

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception:
            emit({"type": "error", "scope": "protocol", "code": "invalid_json", "message": "bad command"})
            continue
        kind = message.get("type")
        vision: Vision = state["vision"]
        if kind == "shutdown":
            vision.stop()
            break
        if kind in ("ocr", "config"):
            if kind == "config":
                vision.config.update(message.get("config") or {})
            if "enabled" in message:
                vision.enabled = bool(message["enabled"])
            emit({"type": "status", "ocr": vision.enabled})
        elif kind == "snapshot":
            vision._snapshot_request.set()
        elif kind == "voice":
            previous = state.get("voice")
            if previous:
                previous.stop()
                state["voice"] = None
            if message.get("enabled") is False:
                continue
            try:
                options = VoiceConfig(**(message.get("config") or {}))
            except TypeError as error:
                emit({"type": "error", "scope": "voice", "code": "bad_config", "message": str(error)})
                continue
            service = VoiceService(options, emit)
            state["voice"] = service
            emit({"type": "status", "voice": service.start()})
        elif kind == "ping":
            emit({"type": "pong", "at": now_iso()})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--interval", type=int, default=1000)
    parser.add_argument("--image", default="")
    parser.add_argument("--layout", default="")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--no-voice", action="store_true")
    parser.add_argument("--voice-config", default="")
    parser.add_argument("--frames", type=int, default=0, help="stop after N frames (debug)")
    args = parser.parse_args()

    enable_dpi_awareness()
    root = Path(args.root)
    config = {"ocrEnabled": not args.no_ocr, "requireForeground": True, "imagePath": args.image, "layoutPath": args.layout}
    vision = Vision(root, config)

    warm = -1.0
    try:
        warm = vision.ocr.warmup()
    except Exception as error:
        emit({"type": "error", "scope": "ocr", "code": "engine_unavailable", "message": str(error)})

    state: dict = {"vision": vision, "voice": None}
    devices = []
    try:
        from lolvision.voice import list_devices

        devices = list_devices()
    except Exception:
        pass

    emit({"type": "ready", "version": __version__, "root": str(root),
          "warmupMs": round(warm, 1), "layout": vision.layout.get("version"),
          "windows": [w.as_dict() for w in find_windows()], "devices": devices})

    if not args.no_voice:
        from lolvision.voice import VoiceConfig, VoiceService

        try:
            options = VoiceConfig(**(json.loads(args.voice_config) if args.voice_config else {}))
        except Exception:
            options = VoiceConfig()
        service = VoiceService(options, emit)
        state["voice"] = service
        emit({"type": "status", "voice": service.start()})

    reader = threading.Thread(target=read_commands, args=(state,), daemon=True)
    reader.start()

    try:
        if args.frames:
            for _ in range(args.frames):
                frame = vision.capture_frame(force=True)
                if frame:
                    emit(frame)
                time.sleep(0.1)
        else:
            vision.loop(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        vision.stop()
        if state.get("voice") is not None:
            state["voice"].stop()
        emit({"type": "exit"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
