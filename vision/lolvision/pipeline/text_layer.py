"""Text rule layer: area-anchored fields, multi-frame fusion, validation.

Input is already-recognised text (text + confidence + box).  This layer decides
which field a block belongs to, whether it parses, whether repeated frames agree,
and whether a sudden jump is physically plausible for that field.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from typing import Any

from .types import FieldCandidate, FieldState, OcrBlock


def _parse_time(match: re.Match, text: str) -> float | None:
    groups = [group for group in match.groups() if group is not None]
    try:
        if len(groups) >= 2:
            minutes, seconds = int(groups[0]), int(groups[1])
            if not (0 <= minutes <= 180 and 0 <= seconds < 60):
                return None
            return minutes * 60 + seconds
        if len(groups) == 1:
            value = int(groups[0])
            return value if 0 <= value <= 10800 else None
    except (TypeError, ValueError):
        return None
    return None


class TextLayer:
    def __init__(self, game: dict, app: dict, unmatched_limit: int = 200) -> None:
        self.fields = {field["name"]: field for field in game["text_fields"]}
        self.order = [field["name"] for field in game["text_fields"]]
        self.fusion_defaults = app.get("fusion", {})
        self.ocr_defaults = app.get("ocr", {})
        self.unmatched_limit = unmatched_limit
        self.history: dict[str, deque] = {name: deque(maxlen=64) for name in self.fields}
        self.accepted: dict[str, FieldState] = {}
        self.previous_lists: dict[str, list[str]] = {}
        self.unmatched: deque = deque(maxlen=unmatched_limit)

    # ------------------------------------------------------------------ parse
    def _candidate(self, field: dict, block: OcrBlock, now_ms: int) -> tuple[FieldCandidate | None, str | None]:
        text = (block.text or "").strip()
        if not text:
            return None, "empty"
        threshold = max(float(field.get("confidence_min", 0.35)), float(self.ocr_defaults.get("min_confidence", 0.0)))
        if block.confidence < threshold:
            return None, "low_confidence"
        pattern = field.get("pattern")
        match = re.search(pattern, text) if pattern else None
        if pattern and not match:
            return None, "pattern"
        raw_value: Any
        kind = field.get("type", "string")
        try:
            if kind == "time":
                raw_value = _parse_time(match, text) if match else None
            elif kind in ("int", "float"):
                token = match.group(1) if (match and match.groups()) else (match.group(0) if match else text)
                token = re.sub(r"[^0-9.\-]", "", token or "")
                if not token or token in {"-", ".", "-."}:
                    return None, "pattern"
                raw_value = float(token)
                if kind == "int":
                    raw_value = int(round(raw_value))
            elif kind == "enum":
                mapping = field.get("enum") or {}
                token = (match.group(1) if (match and match.groups()) else (match.group(0) if match else text)).strip()
                raw_value = mapping.get(token)
                if raw_value is None:
                    raw_value = next((value for key, value in mapping.items() if key in token), None)
                if raw_value is None:
                    return None, "enum"
            else:
                raw_value = (match.group(1) if (match and match.groups()) else (match.group(0) if match else text)).strip()
        except (ValueError, AttributeError):
            return None, "pattern"
        if raw_value is None:
            return None, "pattern"
        if isinstance(raw_value, (int, float)):
            bounds = field.get("range")
            if bounds and not (float(bounds[0]) <= float(raw_value) <= float(bounds[1])):
                return None, "range"
        return FieldCandidate(value=raw_value, raw=text, confidence=block.confidence, timestamp=now_ms), None

    # ------------------------------------------------------------------ fuse
    def _fuse(self, field: dict, now_ms: int) -> tuple[FieldState | None, dict | None]:
        name = field["name"]
        history = self.history[name]
        settings = field.get("multi_frame") or {}
        window = max(1, int(settings.get("window", self.fusion_defaults.get("window", 1))))
        strategy = settings.get("strategy", "mode")
        min_agreement = max(1, int(settings.get("min_agreement", self.fusion_defaults.get("min_agreement", 1))))
        min_agreement = min(min_agreement, window)
        max_age = int(self.fusion_defaults.get("max_age_ms", 4000))
        while history and now_ms - history[0][0] > max_age:
            history.popleft()
        samples = list(history)[-window:]
        if not samples:
            return None, None

        values = [value for _, value, _ in samples]
        confidence = sum(conf for _, _, conf in samples) / len(samples)
        if strategy == "last":
            value = values[-1]
            agreement = 1
        elif strategy == "median":
            numbers = [float(value) for value in values if isinstance(value, (int, float))]
            if not numbers:
                return None, {"field": name, "reason": "not_numeric", "values": values}
            numbers.sort()
            value = numbers[len(numbers) // 2]
            if isinstance(field.get("type"), str) and field.get("type") == "int":
                value = int(round(value))
            tolerance = float(settings.get("tolerance", 0.02))
            scale = max(1.0, abs(value))
            agreement = sum(1 for number in numbers if abs(number - value) <= tolerance * scale)
        else:
            try:
                counts = Counter(values)
            except TypeError:
                return None, {"field": name, "reason": "unhashable", "values": values}
            value, agreement = counts.most_common(1)[0]
        if agreement < min_agreement:
            return None, {"field": name, "reason": "disagreement", "values": values,
                          "agreement": agreement, "required": min_agreement}

        previous = self.accepted.get(name)
        rate = field.get("rate") or {}
        limit = rate.get("max_delta_per_sec")
        if previous is not None and limit is not None and isinstance(value, (int, float)) and isinstance(previous.value, (int, float)):
            elapsed = max(0.001, (now_ms - previous.timestamp) / 1000.0)
            delta = abs(float(value) - float(previous.value))
            if delta > float(limit) * elapsed + float(rate.get("tolerance", 0)):
                return None, {"field": name, "reason": "rate", "from": previous.value, "to": value,
                              "delta": round(delta, 3), "limit_per_sec": limit}
        return FieldState(name=name, value=value, confidence=confidence, timestamp=now_ms, samples=len(samples)), None

    # --------------------------------------------------------------- observe
    def observe(self, blocks: list[OcrBlock], now_ms: int) -> tuple[dict[str, FieldState], list[dict], list[dict], dict[str, list[str]]]:
        grouped: dict[str, list[OcrBlock]] = {}
        unmatched: list[dict] = []
        for block in blocks:
            area = block.area
            if area is None or area not in self.fields:
                unmatched.append({**block.as_dict(), "reason": "no_field", "timestamp": now_ms})
                continue
            grouped.setdefault(area, []).append(block)

        unstable: list[dict] = []
        appeared: dict[str, list[str]] = {}
        for name in self.order:
            field = self.fields[name]
            if field.get("enabled") is False:
                continue
            candidates: list[FieldCandidate] = []
            for block in grouped.get(name, []):
                candidate, reason = self._candidate(field, block, now_ms)
                if candidate is None:
                    unmatched.append({**block.as_dict(), "reason": reason or "invalid", "timestamp": now_ms})
                else:
                    candidates.append(candidate)
            if field.get("collect") == "list":
                self._observe_list(field, candidates, now_ms, appeared)
                continue
            if candidates:
                best = max(candidates, key=lambda item: item.confidence)
                self.history[name].append((now_ms, best.value, best.confidence))
            state, problem = self._fuse(field, now_ms)
            if problem:
                unstable.append({**problem, "timestamp": now_ms})
                continue
            if state is None:
                previous = self.accepted.get(name)
                fallback = field.get("fallback") or {}
                max_age = int(fallback.get("max_age_ms", self.fusion_defaults.get("max_age_ms", 4000)))
                if fallback.get("policy", "hold_last") == "hold_last" and previous and now_ms - previous.timestamp <= max_age:
                    self.accepted[name] = FieldState(
                        name=name, value=previous.value, confidence=previous.confidence,
                        timestamp=previous.timestamp, fresh=False, samples=previous.samples,
                    )
                continue
            self.accepted[name] = state

        for item in unmatched[-self.unmatched_limit:]:
            self.unmatched.append(item)
        ordered = {name: self.accepted[name] for name in self.order if name in self.accepted}
        return ordered, unstable, list(self.unmatched), appeared

    def _observe_list(self, field: dict, candidates: list[FieldCandidate], now_ms: int, appeared: dict[str, list[str]]) -> None:
        name = field["name"]
        current = [candidate.value for candidate in candidates if isinstance(candidate.value, str)]
        previous = self.previous_lists.get(name, [])
        fresh = [value for value in current if value not in previous]
        if fresh:
            appeared[name] = fresh
        merged: list[str] = []
        for value in current + previous:
            if value not in merged:
                merged.append(value)
        cap = int(field.get("max_items", 6))
        merged = merged[:cap]
        self.previous_lists[name] = merged
        self.accepted[name] = FieldState(
            name=name, value=merged, confidence=max([c.confidence for c in candidates], default=0.0),
            timestamp=now_ms, samples=len(candidates),
        )

    def reset(self) -> None:
        for history in self.history.values():
            history.clear()
        self.accepted.clear()
        self.previous_lists.clear()
        self.unmatched.clear()

    def drain_unmatched(self) -> list[dict]:
        items = list(self.unmatched)
        self.unmatched.clear()
        return items

    @staticmethod
    def serialize(state: dict[str, FieldState]) -> dict[str, Any]:
        return {name: item.value for name, item in state.items()}
