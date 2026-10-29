"""Shared data types for the recognition pipeline.

The pipeline never owns capture or OCR: it receives OCR blocks and image
frames from the existing sidecar and turns them into state, events and changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rect:
    """Normalised rectangle (0..1) relative to the game client area."""

    x: float
    y: float
    w: float
    h: float

    @classmethod
    def from_list(cls, value, where: str = "rect") -> "Rect":
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError(f"{where} must be [x, y, w, h]")
        x, y, w, h = (float(item) for item in value)
        for item in (x, y, w, h):
            if not -1.0 <= item <= 2.0:
                raise ValueError(f"{where} values must be normalised fractions")
        if w <= 0 or h <= 0:
            raise ValueError(f"{where} width and height must be positive")
        return cls(x, y, w, h)

    def shifted(self, dx: float = 0.0, dy: float = 0.0) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def pixels(self, size: tuple[int, int], dx: float = 0.0, dy: float = 0.0) -> tuple[int, int, int, int]:
        width, height = size
        left = int(round((self.x + dx) * width))
        top = int(round((self.y + dy) * height))
        pixel_w = max(1, int(round(self.w * width)))
        pixel_h = max(1, int(round(self.h * height)))
        left = max(0, min(width - 1, left))
        top = max(0, min(height - 1, top))
        pixel_w = min(pixel_w, width - left)
        pixel_h = min(pixel_h, height - top)
        return left, top, pixel_w, pixel_h

    def as_list(self) -> list[float]:
        return [round(self.x, 4), round(self.y, 4), round(self.w, 4), round(self.h, 4)]


@dataclass
class OcrBlock:
    """One recognised text line, with an absolute pixel box."""

    text: str
    confidence: float
    bbox: tuple[int, int, int, int]
    area: str | None = None

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "confidence": round(float(self.confidence), 3),
            "bbox": list(self.bbox),
            **({"area": self.area} if self.area else {}),
        }


@dataclass
class FieldCandidate:
    """A single frame's parsed value for one configured field."""

    value: Any
    raw: str
    confidence: float
    timestamp: int


@dataclass
class FieldState:
    """Fused, accepted value for one field."""

    name: str
    value: Any
    confidence: float
    timestamp: int
    fresh: bool = True
    samples: int = 1


@dataclass
class DetectorResult:
    name: str
    value: Any
    confidence: float
    timestamp: int
    kind: str = "unknown"
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "confidence": round(float(self.confidence), 3),
            "timestamp": self.timestamp,
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass
class PipelineEvent:
    name: str
    timestamp: int
    detail: dict = field(default_factory=dict)
    severity: str = "info"
    count: int = 1
    id: str = ""

    def as_dict(self) -> dict:
        payload = {
            "id": self.id,
            "event": self.name,
            "timestamp": self.timestamp,
            "severity": self.severity,
        }
        if self.count > 1:
            payload["count"] = self.count
        if self.detail:
            payload["detail"] = self.detail
        return payload
