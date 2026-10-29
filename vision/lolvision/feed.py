"""Kill-feed event tracking.

The kill feed lingers on screen for several seconds, so the same row is seen in
many consecutive frames.  Each row therefore gets a stable ``EventID`` the first
time it appears and keeps it until it leaves the screen; the host's existing
event cursor can then de-duplicate across batches without a second heuristic.
"""

from __future__ import annotations

import re

from . import parse

RETIRE_AFTER_MISSES = 2


def _key(parsed: dict) -> str:
    if parsed.get("kind") == "ObjectiveKill":
        return f"obj:{parsed.get('objective')}"
    if parsed.get("kind") == "BuildingKill":
        return f"bld:{parsed.get('objective')}:{parsed.get('raw', '')[:24]}"
    return f"kill:{parsed.get('killer')}>{parsed.get('victim')}"


class EventTracker:
    def __init__(self) -> None:
        self.seq = 0
        self.active: dict[str, dict] = {}
        self.misses: dict[str, int] = {}
        self.baseline_done = False

    def update(self, texts: list[str], game_time: float | None) -> list[dict]:
        """Return newly detected events for this frame."""
        occurrences: dict[str, int] = {}
        seen: dict[str, dict] = {}
        for text in texts:
            parsed = parse.parse_kill(text)
            if not parsed:
                continue
            base = _key(parsed)
            occurrences[base] = occurrences.get(base, 0) + 1
            key = f"{base}#{occurrences[base]}" if occurrences[base] > 1 else base
            seen[key] = parsed | {"raw": parse.normalise(text)}

        emitted: list[dict] = []
        for key, parsed in seen.items():
            if key in self.active:
                entry = self.active[key]
                entry["misses"] = 0
                self.misses[key] = 0
                continue
            self.seq += 1
            entry = {
                "id": self.seq,
                "key": key,
                "parsed": parsed,
                "eventTime": round(float(game_time), 1) if game_time is not None else None,
                "misses": 0,
            }
            self.active[key] = entry
            self.misses[key] = 0
            # The first frame after start is a baseline: old feed rows are not new.
            if self.baseline_done:
                emitted.append(self._to_event(entry))

        for key in list(self.active):
            if key in seen:
                continue
            self.misses[key] = self.misses.get(key, 0) + 1
            if self.misses[key] >= RETIRE_AFTER_MISSES:
                self.active.pop(key, None)
                self.misses.pop(key, None)

        self.baseline_done = True
        return emitted

    @staticmethod
    def _to_event(entry: dict) -> dict:
        parsed = entry["parsed"]
        kind = parsed.get("kind")
        event: dict = {"EventID": entry["id"]}
        if kind == "ChampionKill":
            event.update({
                "EventName": "ChampionKill",
                "KillerName": parsed.get("killer"),
                "VictimName": parsed.get("victim"),
            })
        elif kind == "SelfDeath":
            event.update({
                "EventName": "ChampionKill",
                "KillerName": parsed.get("killer"),
                "VictimName": "你",
            })
        elif kind == "ObjectiveKill":
            event.update({"EventName": "EpicMonsterKill", "MonsterType": parsed.get("objective")})
        elif kind == "BuildingKill":
            event.update({"EventName": "BuildingKill", "BuildingType": parsed.get("objective")})
        else:
            event.update({"EventName": kind or "Unknown"})
        if entry.get("eventTime") is not None:
            event["EventTime"] = entry["eventTime"]
        event["raw"] = parsed.get("raw")
        event["source"] = "screen_ocr"
        return event

    def reset(self) -> None:
        self.seq = 0
        self.active.clear()
        self.misses.clear()
        self.baseline_done = False
