"""Thin RapidOCR wrapper tuned for fixed HUD slots.

Two modes matter for latency:

``line``  recogniser only (detection skipped).  A cropped HUD line costs a few
          milliseconds, which is what makes a 1 Hz (or faster) poll affordable
          while the game is running.
``det``   full detect + recognise, for text whose position is unknown
          (centre announcements).

RapidOCR 1.2.3 selects detection through its constructor (``use_text_det``), not
through a per-call flag, so two engines are kept and reused.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Line:
    text: str
    score: float
    x: float  # left edge, fraction of the crop width
    y: float  # top edge, fraction of the crop height

    def as_dict(self) -> dict:
        return {"text": self.text, "score": round(self.score, 4),
                "x": round(self.x, 3), "y": round(self.y, 3)}


class OcrEngine:
    def __init__(self, min_score: float = 0.45, threads: int = 3) -> None:
        self.min_score = min_score
        self.threads = threads
        self._line = None
        self._det = None
        self._lock = threading.Lock()
        self.last_ms = {"line": 0.0, "det": 0.0}

    def ensure(self) -> None:
        if self._line is not None and self._det is not None:
            return
        with self._lock:
            from rapidocr_onnxruntime import RapidOCR

            if self._line is None:
                self._line = RapidOCR(use_text_det=False, use_angle_cls=False,
                                      intra_op_num_threads=self.threads)
            if self._det is None:
                self._det = RapidOCR(use_angle_cls=False,
                                     intra_op_num_threads=self.threads)

    def warmup(self) -> float:
        import time

        self.ensure()
        probe = np.full((48, 240, 3), 18, dtype=np.uint8)
        started = time.perf_counter()
        self.read(probe, mode="line")
        self.read(probe, mode="det")
        return (time.perf_counter() - started) * 1000

    # ------------------------------------------------------------------ read
    def read(self, image: np.ndarray, mode: str = "line") -> list[Line]:
        self.ensure()
        if image is None or image.size == 0:
            return []
        prepared = self._prepare(image, mode)
        import time

        started = time.perf_counter()
        try:
            with self._lock:
                if mode == "line":
                    # Call the recogniser directly: RapidOCR's wrapper drops
                    # low-confidence single characters such as a lone "8".
                    result, _elapse = self._line.text_recognizer([prepared])
                    result = [([[0, 0], [prepared.shape[1], 0],
                                [prepared.shape[1], prepared.shape[0]], [0, prepared.shape[0]]],
                               text, score) for text, score in result]
                else:
                    result, _elapse = self._det(prepared)
        except Exception:
            return []
        self.last_ms[mode if mode in self.last_ms else "line"] = (time.perf_counter() - started) * 1000
        return self._normalise(result, prepared.shape[1], prepared.shape[0])

    # ------------------------------------------------------------- internals
    def _prepare(self, image: np.ndarray, mode: str) -> np.ndarray:
        height, width = image.shape[:2]
        # Tiny crops lose accuracy: the recogniser expects ~48 px of text height.
        if mode == "line" and height < 30:
            scale = 30.0 / max(1, height)
            image = cv2.resize(image, (max(1, int(width * scale)), max(1, int(height * scale))),
                               interpolation=cv2.INTER_CUBIC)
        if mode == "line":
            # A neutral border keeps ascenders/descenders from being clipped.
            image = cv2.copyMakeBorder(image, 4, 4, 10, 10, cv2.BORDER_REPLICATE)
        return np.ascontiguousarray(image)
    @staticmethod
    def _normalise(result, width: int, height: int) -> list[Line]:
        lines: list[Line] = []
        if not result:
            return lines
        for item in result:
            try:
                box, text, score = item[0], str(item[1]).strip(), float(item[2])
            except Exception:
                continue
            if not text:
                continue
            try:
                xs = [float(point[0]) for point in box]
                ys = [float(point[1]) for point in box]
                x = min(xs) / max(1, width)
                y = min(ys) / max(1, height)
            except Exception:
                x = y = 0.0
            lines.append(Line(text=text, score=score, x=x, y=y))
        return lines
