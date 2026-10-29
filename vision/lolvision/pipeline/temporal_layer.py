"""Temporal event layer.

Turns per-frame state into an event stream using only configuration: threshold
crossings, deltas, detector flags and newly appeared text, each with a
sustained-frame count, a cooldown and an optional merge window.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from .types import PipelineEvent


@dataclass
class _RuleState:
    streak: int = 0
    last_fired: int | None = None
    event: PipelineEvent | None = field(default=None)


class TemporalLayer:
    def __init__(self, game: dict, app: dict) -> None:
        self.rules = [rule for rule in game["events"] if rule.get("enabled", True)]
        defaults = app.get("events", {})
        self.cooldown_default = int(defaults.get("cooldown_default_ms", 4000))
        self.merge_default = int(defaults.get("merge_window_ms", 1500))
        self.states: dict[str, _RuleState] = {rule["name"]: _RuleState() for rule in self.rules}
        self.previous_values: dict[str, Any] = {}

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _changes(self, values: dict, detectors: dict, precision: int) -> list[dict]:
        changes: list[dict] = []
        combined = {**values, **detectors}
        for name, value in combined.items():
            before = self.previous_values.get(name, "__missing__")
            if before == "__missing__":
                continue
            if isinstance(value, list) or isinstance(before, list):
                old = before if isinstance(before, list) else []
                new = value if isinstance(value, list) else []
                added = [item for item in new if item not in old]
                removed = [item for item in old if item not in new]
                if added or removed:
                    changes.append({"field": name, "added": added, "removed": removed})
                continue
            if isinstance(value, bool) or isinstance(before, bool):
                if bool(value) != bool(before):
                    changes.append({"field": name, "to": bool(value)})
                continue
            a, b = self._number(before), self._number(value)
            if a is None or b is None:
                if value != before:
                    changes.append({"field": name, "from": before, "to": value})
                continue
            delta = round(b - a, precision)
            if delta != 0:
                changes.append({"field": name, "delta": delta, "from": round(a, precision), "to": round(b, precision)})
        self.previous_values = {**values, **detectors}
        return changes

    def _condition(self, rule: dict, combined: dict, appeared: dict) -> tuple[bool, dict]:
        when = rule["when"]
        name = when["field"]
        kind = when["type"]
        detail: dict = {"field": name}
        if kind == "appears":
            entries = appeared.get(name) or []
            pattern = when.get("pattern")
            matches = [entry for entry in entries if not pattern or re.search(pattern, entry)]
            if not matches:
                return False, detail
            detail["texts"] = matches[:5]
            return True, detail
        value = combined.get(name)
        if kind == "bool":
            expected = bool(when.get("value", True))
            if isinstance(value, bool) and value == expected:
                return True, {"field": name, "value": value}
            return False, detail
        number = self._number(value)
        if number is None:
            return False, detail
        if kind == "threshold":
            above = when.get("above")
            below = when.get("below")
            if above is None and below is None:
                return False, detail
            if above is not None and number <= float(above):
                return False, detail
            if below is not None and number >= float(below):
                return False, detail
            detail.update({"value": round(number, 4), **({"above": above} if above is not None else {}),
                           **({"below": below} if below is not None else {})})
            return True, detail
        if kind == "delta":
            before = self._number(self.previous_values.get(name))
            if before is None:
                return False, detail
            delta = number - before
            low = when.get("delta_below")
            high = when.get("delta_above")
            if low is not None and delta > float(low):
                return False, detail
            if high is not None and delta < float(high):
                return False, detail
            detail.update({"delta": round(delta, 4), "from": round(before, 4), "to": round(number, 4)})
            return True, detail
        return False, detail

    # ---------------------------------------------------------------- observe
    def observe(self, values: dict, detector_values: dict, appeared: dict, now_ms: int,
                precision: int = 3) -> tuple[list[dict], list[dict]]:
        changes = self._changes(values, detector_values, precision)
        combined = {**values, **detector_values}
        emitted: list[dict] = []

        for rule in self.rules:
            name = rule["name"]
            state = self.states[name]
            ok, detail = self._condition(rule, combined, appeared)
            state.streak = state.streak + 1 if ok else 0
            if not ok:
                continue
            sustained = max(1, int(rule.get("sustained_frames", 1)))
            if state.streak < sustained:
                continue
            if state.streak > sustained and rule.get("once_per_streak", True):
                continue

            cooldown = int(rule.get("cooldown_ms") if rule.get("cooldown_ms") is not None else self.cooldown_default)
            merge = int(rule.get("merge_window_ms") if rule.get("merge_window_ms") is not None else self.merge_default)
            since = None if state.last_fired is None else now_ms - state.last_fired

            if since is not None and since < merge and state.event is not None:
                state.event.count += 1
                state.event.detail = {**state.event.detail, **detail}
                state.last_fired = now_ms
                emitted.append(state.event.as_dict())
                continue
            if since is not None and since < cooldown:
                continue

            event = PipelineEvent(
                name=name, timestamp=now_ms, detail=detail,
                severity=str(rule.get("severity", "info")),
                id=uuid.uuid4().hex[:12],
            )
            state.event = event
            state.last_fired = now_ms
            emitted.append(event.as_dict())
        return emitted, changes

    def reset(self) -> None:
        for state in self.states.values():
            state.streak = 0
            state.last_fired = None
            state.event = None
        self.previous_values.clear()

    def status(self) -> dict:
        return {
            "rules": {name: {"streak": state.streak, "lastFired": state.last_fired}
                      for name, state in self.states.items()},
        }
