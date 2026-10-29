"""Graphical CV layer.

Four configurable detector families run on the captured frame only:
colour coverage, bar fill, template presence, and region change.  They are
intentionally cheap (no OCR, no model) and share a per-frame time budget.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .types import DetectorResult


def _hsv_range(config: dict) -> tuple[np.ndarray, np.ndarray]:
    low, high = config["color"]["range"]
    return np.array(low, dtype=np.uint8), np.array(high, dtype=np.uint8)


def _mask(image: np.ndarray, config: dict) -> np.ndarray:
    space = str((config.get("color") or {}).get("space", "hsv")).lower()
    if space == "hsv":
        converted = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    elif space == "bgr":
        converted = image
    else:
        converted = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        converted = cv2.cvtColor(converted, cv2.COLOR_GRAY2BGR)
    low, high = _hsv_range(config)
    mask = cv2.inRange(converted, low, high)
    kernel = int((config.get("morphology") or {}).get("kernel", 0))
    if kernel >= 2:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((kernel, kernel), np.uint8))
    return mask


def colour_ratio(image: np.ndarray, config: dict) -> DetectorResult | None:
    mask = _mask(image, config)
    if mask.size == 0:
        return None
    coverage = float(mask.mean() / 255.0)
    expected = float((config.get("color") or {}).get("expected_min", 0.05))
    confidence = min(1.0, coverage / max(1e-6, expected))
    return DetectorResult(name=config["name"], value=round(coverage, 4), confidence=confidence,
                          timestamp=0, kind="color")


def bar_fill(image: np.ndarray, config: dict) -> DetectorResult | None:
    mask = _mask(image, config)
    if mask.size == 0:
        return None
    column_coverage = mask.mean(axis=0) / 255.0
    filled = column_coverage > float(config.get("column_threshold", 0.5))
    if not filled.any():
        return DetectorResult(name=config["name"], value=0.0, confidence=1.0, timestamp=0, kind="bar",
                              detail={"reason": "empty"})
    best = current = 0
    for value in filled:
        current = current + 1 if value else 0
        best = max(best, current)
    ratio = best / max(1, filled.size)
    # A solid run means the fill edge was unambiguous.
    run_pixels = float(mask[:, np.where(filled)[0]].mean() / 255.0) if filled.any() else 0.0
    return DetectorResult(name=config["name"], value=round(min(1.0, ratio), 4), confidence=min(1.0, run_pixels),
                          timestamp=0, kind="bar", detail={"run_px": int(run_pixels * mask.shape[0])})


class CvLayer:
    def __init__(self, game: dict, app: dict, root: Path | str) -> None:
        self.detectors = [dict(item) for item in game["cv_detectors"] if item.get("enabled", True)]
        self.root = Path(root)
        self.budget_ms = float(app.get("cv", {}).get("max_frame_ms", 50))
        self.enabled = bool(app.get("cv", {}).get("enabled", True))
        self._templates: dict[str, np.ndarray] = {}
        self._previous: dict[str, np.ndarray] = {}
        self.last_timing: dict[str, float] = {}

    def _template(self, relative: str) -> np.ndarray | None:
        if relative in self._templates:
            return self._templates[relative] or None
        path = Path(relative)
        if not path.is_absolute():
            path = self.root / relative
        if not path.exists():
            self._templates[relative] = np.zeros(0, dtype=np.uint8)
            return None
        image = cv2.imread(str(path))
        self._templates[relative] = image if image is not None else np.zeros(0, dtype=np.uint8)
        return image

    def _run_one(self, detector: dict, crop: np.ndarray, now_ms: int) -> DetectorResult | None:
        kind = detector.get("type")
        name = detector["name"]
        if kind == "color":
            result = colour_ratio(crop, detector)
        elif kind == "bar":
            result = bar_fill(crop, detector)
        elif kind == "template":
            template = self._template(detector["template"])
            if template is None or template.size == 0:
                return DetectorResult(name=name, value=None, confidence=0.0, timestamp=now_ms, kind="template",
                                      detail={"error": "template_missing", "path": detector["template"]})
            if template.shape[0] > crop.shape[0] or template.shape[1] > crop.shape[1]:
                return DetectorResult(name=name, value=False, confidence=0.0, timestamp=now_ms, kind="template",
                                      detail={"error": "template_larger_than_area"})
            scores = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
            _, best, _, location = cv2.minMaxLoc(scores)
            threshold = float(detector.get("threshold", 0.8))
            return DetectorResult(name=name, value=bool(best >= threshold), confidence=float(max(0.0, best)),
                                  timestamp=now_ms, kind="template",
                                  detail={"score": round(float(best), 4), "at": list(location)})
        elif kind == "change":
            previous = self._previous.get(name)
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            if previous is not None and previous.shape == gray.shape:
                diff = cv2.absdiff(gray, previous)
                value = float(diff.mean()) / 255.0
                confidence = 1.0
            else:
                value, confidence = 0.0, 0.0
            self._previous[name] = gray
            return DetectorResult(name=name, value=round(value, 4), confidence=confidence,
                                  timestamp=now_ms, kind="change")
        else:
            return DetectorResult(name=name, value=None, confidence=0.0, timestamp=now_ms,
                                  kind=str(kind), detail={"error": "unknown_type"})
        if result is not None:
            result.timestamp = now_ms
        return result

    def observe(self, frame: np.ndarray, crops: dict[str, np.ndarray], now_ms: int) -> tuple[dict[str, DetectorResult], list[str]]:
        """Run detectors in configuration order until the time budget is spent."""
        results: dict[str, DetectorResult] = {}
        skipped: list[str] = []
        self.last_timing = {}
        if not self.enabled:
            return results, [detector["name"] for detector in self.detectors]
        started = time.perf_counter()
        for index, detector in enumerate(self.detectors):
            if (time.perf_counter() - started) * 1000 > self.budget_ms:
                skipped.extend(item["name"] for item in self.detectors[index:])
                break
            crop = crops.get(detector["name"])
            if crop is None or crop.size == 0:
                skipped.append(detector["name"])
                continue
            tick = time.perf_counter()
            try:
                result = self._run_one(detector, crop, now_ms)
            except cv2.error:
                result = DetectorResult(name=detector["name"], value=None, confidence=0.0,
                                        timestamp=now_ms, kind=str(detector.get("type")),
                                        detail={"error": "cv_failed"})
            self.last_timing[detector["name"]] = round((time.perf_counter() - tick) * 1000, 2)
            if result is not None:
                results[detector["name"]] = result
        return results, skipped

    def reset(self) -> None:
        self._previous.clear()

    @staticmethod
    def serialize(results: dict[str, DetectorResult]) -> dict[str, Any]:
        return {name: result.value for name, result in results.items() if result.value is not None}
