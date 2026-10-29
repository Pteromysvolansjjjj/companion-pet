"""Turn OCR text into the fields the plugin's data protocol expects."""

from __future__ import annotations

import re
import unicodedata

FULLWIDTH = str.maketrans({
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4", "５": "5", "６": "6",
    "７": "7", "８": "8", "９": "9", "：": ":", "／": "/", "．": ".", "－": "-",
    "，": ",", "（": "(", "）": ")", "｜": "|", "　": " ",
})

CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)")
BARE_CLOCK_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(?!\d)")
KDA_RE = re.compile(r"(\d{1,2})\s*[/／]\s*(\d{1,2})\s*[/／]\s*(\d{1,2})")
INT_RE = re.compile(r"(?<!\d)(\d{1,3})(?!\d)")

KILL_PATTERNS = [
    # 「X 击杀了 Y」 / 「X 已经击杀了 Y」 / 「X 终结了 Y」
    re.compile(r"^(?P<killer>.{1,24}?)\s*(?:已经)?(?:击杀了|终结了|杀死了)\s*(?P<victim>.{1,24})$"),
    # 「Y 被 X 击杀」
    re.compile(r"^(?P<victim>.{1,24}?)\s*被\s*(?P<killer>.{1,24}?)\s*(?:击杀|终结)"),
]
SELF_DEATH_RE = re.compile(r"你已被\s*(?P<killer>.{1,24}?)\s*击杀")

OBJECTIVE_WORDS = ("巨龙", "小龙", "男爵", "纳什", "峡谷先锋", "先锋", "远古龙", "大龙", "海克斯龙")
TOWER_WORDS = ("防御塔", "水晶", "兵营")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).translate(FULLWIDTH)
    return re.sub(r"\s+", " ", text).strip()


def parse_clock(text: str) -> int | None:
    """Return game time in whole seconds, or None."""
    match = CLOCK_RE.search(text)
    if match:
        minutes, seconds = int(match.group(1)), int(match.group(2))
        if 0 <= minutes <= 180 and 0 <= seconds < 60:
            return minutes * 60 + seconds
        return None
    match = BARE_CLOCK_RE.search(text)
    if match:
        minutes, seconds = int(match.group(1)), int(match.group(2))
        if 0 <= minutes <= 180 and 0 <= seconds < 60:
            return minutes * 60 + seconds
    return None


def parse_top_bar(text: str) -> dict:
    """Read ``<ally kills> <mm:ss> <enemy kills>`` from the centred top bar."""
    cleaned = normalise(text)
    result: dict = {}
    clock = CLOCK_RE.search(cleaned)
    if clock:
        seconds = parse_clock(cleaned[clock.start():clock.end()])
        if seconds is not None:
            result["gameTime"] = seconds
        before = [int(m.group(1)) for m in INT_RE.finditer(cleaned[: clock.start()])]
        after = [int(m.group(1)) for m in INT_RE.finditer(cleaned[clock.end():])]
    else:
        seconds = parse_clock(cleaned)
        if seconds is not None:
            result["gameTime"] = seconds
        before, after = [], []
        numbers = [int(m.group(1)) for m in INT_RE.finditer(cleaned)]
        if len(numbers) >= 3:
            before, after = [numbers[0]], [numbers[-1]]
    if before:
        result["allyKills"] = before[-1] % 1000
    if after:
        result["enemyKills"] = after[0] % 1000
    return result


def parse_kill(text: str) -> dict | None:
    """Classify one kill-feed line."""
    cleaned = normalise(text)
    if len(cleaned) < 3:
        return None
    self_death = SELF_DEATH_RE.search(cleaned)
    if self_death:
        return {"kind": "SelfDeath", "killer": clean_name(self_death.group("killer")), "victim": "你"}
    for pattern in KILL_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            killer, victim = clean_name(match.group("killer")), clean_name(match.group("victim"))
            if not killer or not victim:
                continue
            return {"kind": "ChampionKill", "killer": killer, "victim": victim}
    for word in OBJECTIVE_WORDS:
        if word in cleaned and any(token in cleaned for token in ("击杀", "拿下", "已夺取")):
            return {"kind": "ObjectiveKill", "objective": word, "raw": cleaned}
    for word in TOWER_WORDS:
        if word in cleaned and any(token in cleaned for token in ("摧毁", "被摧毁", "丢失")):
            return {"kind": "BuildingKill", "objective": word, "raw": cleaned}
    return None


def clean_name(value: str) -> str:
    value = normalise(value)
    value = re.sub(r"^[\[\(【（]?[^\]\)】）]{0,3}[\]\)】）]\s*", "", value)
    value = re.sub(r"[（(].*?[）)]", "", value)
    return value.strip(" :：-—·•.。!！,，")


def parse_kda(text: str) -> tuple[int, int, int] | None:
    match = KDA_RE.search(normalise(text))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_level(text: str) -> int | None:
    cleaned = normalise(text)
    match = re.fullmatch(r"(\d{1,2})", cleaned)
    if not match:
        return None
    level = int(match.group(1))
    return level if 1 <= level <= 18 else None


def parse_board_texts(texts: list[str]) -> dict:
    """Parse scoreboard rows that were read one slot at a time."""
    rows = []
    for raw in texts:
        text = normalise(raw)
        if not text:
            continue
        match = KDA_RE.search(text)
        row: dict = {"text": text, "cells": [text]}
        if match:
            row.update({"kills": int(match.group(1)), "deaths": int(match.group(2)),
                        "assists": int(match.group(3))})
            tail = text[match.end():]
            numbers = [int(m.group(1)) for m in INT_RE.finditer(tail)]
            if numbers:
                row["cs"] = numbers[0]
        rows.append(row)
    return {"rows": rows}


def parse_scoreboard(lines: list) -> dict:
    """Group OCR lines into table rows and pull KDA/CS out of each row."""
    entries = []
    for line in lines:
        text = normalise(getattr(line, "text", line))
        if not text:
            continue
        entries.append((getattr(line, "y", 0.0), getattr(line, "x", 0.0), text))
    entries.sort(key=lambda item: (round(item[0], 2), item[1]))

    rows: list[dict] = []
    for y, x, text in entries:
        if rows and abs(rows[-1]["y"] - y) <= 0.012:
            rows[-1]["cells"].append(text)
        else:
            rows.append({"y": y, "cells": [text]})
    result_rows = []
    for row in rows:
        joined = " ".join(row["cells"])
        kda = parse_kda(joined)
        cell_kda = next((i for i, cell in enumerate(row["cells"]) if parse_kda(cell)), None)
        cs = None
        if cell_kda is not None:
            for cell in row["cells"][cell_kda + 1:]:
                match = re.fullmatch(r"(\d{1,3})", cell.strip())
                if match:
                    cs = int(match.group(1))
                    break
        entry = {"cells": row["cells"], "text": joined}
        if kda:
            entry.update({"kills": kda[0], "deaths": kda[1], "assists": kda[2]})
        if cs is not None:
            entry["cs"] = cs
        result_rows.append(entry)
    return {"rows": result_rows}
