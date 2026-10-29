"""YAML configuration loading and validation.

Two files drive everything:

``configs/app.yaml``        runtime switches, fusion defaults, vision fallback
``configs/games/<id>.yaml`` anchors, text fields, CV detectors, event rules

Nothing in the pipeline hard-codes a coordinate, a threshold or a pattern.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from .types import Rect

GAME_ROOT = "configs/games"

APP_DEFAULTS: dict = {
    "game": "lol",
    "base_resolution": [1920, 1080],
    "capture": {"interval_ms": 1000, "require_foreground": True},
    "ocr": {"enabled": True, "min_confidence": 0.35, "max_area_ms": 250},
    "cv": {"enabled": True, "max_frame_ms": 50},
    "fusion": {"window": 3, "min_agreement": 2, "max_age_ms": 4000},
    "events": {"cooldown_default_ms": 4000, "merge_window_ms": 1500},
    "output": {"include_unmatched": True, "include_raw_fields": False, "float_precision": 3},
    "vision": {
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "model": "placeholder-vision-model",
        "api_key_env": "VISION_API_KEY",
        "timeout_ms": 8000,
        "min_interval_ms": 30000,
        "max_calls_per_hour": 30,
        "max_tokens": 120,
        "on_events": ["death", "kill", "round_end"],
        "prompt": "用一句话描述这张游戏画面里正在发生什么，只描述可见内容，不推测敌人位置，不给操作建议。",
    },
}

FIELD_DEFAULTS: dict = {
    "type": "string",
    "confidence_min": 0.35,
    "multi_frame": {"window": 3, "strategy": "mode", "min_agreement": 2, "tolerance": 0.02},
    "fallback": {"policy": "hold_last", "max_age_ms": 4000},
    "collect": "first",
    "enabled": True,
}

DETECTOR_DEFAULTS: dict = {
    "enabled": True,
    "confidence_min": 0.0,
}

EVENT_DEFAULTS: dict = {
    "enabled": True,
    "sustained_frames": 1,
    "cooldown_ms": None,
    "merge_window_ms": None,
    "severity": "info",
}

VALID_TYPES = {"int", "float", "time", "string", "enum"}
VALID_STRATEGIES = {"mode", "median", "last"}
VALID_COLLECT = {"first", "list"}
VALID_DETECTORS = {"color", "bar", "template", "change"}
VALID_FALLBACK = {"hold_last", "none"}


class ConfigError(ValueError):
    pass


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"配置不存在: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigError(f"{path.name} 不是合法 YAML: {error}") from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} 顶层必须是映射")
    return data


def _deep_merge(base: dict, overlay: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def load_app(path: Path | str) -> dict:
    path = Path(path)
    data = _deep_merge(APP_DEFAULTS, _read_yaml(path))

    _require(isinstance(data["game"], str) and data["game"], "app.game 必须是非空字符串")
    base = data["base_resolution"]
    _require(isinstance(base, (list, tuple)) and len(base) == 2 and all(int(v) > 0 for v in base),
             "app.base_resolution 必须是 [宽, 高]")
    interval = data["capture"]["interval_ms"]
    _require(isinstance(interval, int) and interval >= 50, "capture.interval_ms 必须是 >=50 的整数")
    for section in ("ocr", "cv", "fusion", "events", "output", "vision"):
        _require(isinstance(data[section], dict), f"app.{section} 必须是映射")
    _require(data["fusion"]["window"] >= 1, "fusion.window 必须 >=1")
    _require(data["fusion"]["min_agreement"] >= 1, "fusion.min_agreement 必须 >=1")
    _require(isinstance(data["vision"]["api_key_env"], str) and data["vision"]["api_key_env"],
             "vision.api_key_env 必须给出环境变量名")
    _require(data["vision"]["min_interval_ms"] >= 1000, "vision.min_interval_ms 不能小于 1 秒")
    data["_path"] = str(path.resolve())
    return data


def _validate_field(field: dict, index: int) -> dict:
    where = f"text_fields[{index}]"
    _require(isinstance(field, dict), f"{where} 必须是映射")
    merged = _deep_merge(FIELD_DEFAULTS, field)
    name = merged.get("name")
    _require(isinstance(name, str) and name, f"{where}.name 必须是非空字符串")
    where = f"text_fields[{name}]"
    if "area" in merged:
        merged["_rect"] = Rect.from_list(merged["area"], f"{where}.area")
    elif merged.get("anchor"):
        _require("offset" in merged, f"{where} 使用 anchor 时必须给出 offset")
        _require(isinstance(merged["offset"], (list, tuple)) and len(merged["offset"]) == 2,
                 f"{where}.offset 必须是 [dx, dy]")
        _require(isinstance(merged.get("size"), (list, tuple)) and len(merged["size"]) == 2,
                 f"{where} 使用 anchor 时必须给出 size=[w, h]")
        merged["_rect"] = Rect(0.0, 0.0, float(merged["size"][0]), float(merged["size"][1]))
    else:
        raise ConfigError(f"{where} 需要 area 或 anchor+offset+size")
    _require(merged["type"] in VALID_TYPES, f"{where}.type 必须是 {sorted(VALID_TYPES)}")
    _require(merged["collect"] in VALID_COLLECT, f"{where}.collect 必须是 {sorted(VALID_COLLECT)}")
    pattern = merged.get("pattern")
    if pattern is not None:
        _require(isinstance(pattern, str), f"{where}.pattern 必须是字符串")
        import re
        try:
            re.compile(pattern)
        except re.error as error:
            raise ConfigError(f"{where}.pattern 不是合法正则: {error}") from error
    frames = merged.get("multi_frame") or {}
    _require(frames.get("strategy") in VALID_STRATEGIES, f"{where}.multi_frame.strategy 必须是 {sorted(VALID_STRATEGIES)}")
    _require(int(frames.get("window", 1)) >= 1, f"{where}.multi_frame.window 必须 >=1")
    _require(merged["fallback"]["policy"] in VALID_FALLBACK, f"{where}.fallback.policy 必须是 {sorted(VALID_FALLBACK)}")
    if merged.get("range") is not None:
        rng = merged["range"]
        _require(isinstance(rng, (list, tuple)) and len(rng) == 2 and rng[0] <= rng[1],
                 f"{where}.range 必须是 [最小值, 最大值]")
    if merged.get("enum") is not None:
        _require(isinstance(merged["enum"], dict) and merged["enum"], f"{where}.enum 必须是非空映射")
        _require(merged["type"] == "enum", f"{where}.enum 只能用于 type: enum")
    return merged


def _validate_detector(detector: dict, index: int) -> dict:
    where = f"cv_detectors[{index}]"
    _require(isinstance(detector, dict), f"{where} 必须是映射")
    merged = _deep_merge(DETECTOR_DEFAULTS, detector)
    name = merged.get("name")
    _require(isinstance(name, str) and name, f"{where}.name 必须是非空字符串")
    where = f"cv_detectors[{name}]"
    kind = merged.get("type")
    _require(kind in VALID_DETECTORS, f"{where}.type 必须是 {sorted(VALID_DETECTORS)}")
    _require("area" in merged, f"{where} 需要 area")
    merged["_rect"] = Rect.from_list(merged["area"], f"{where}.area")
    if kind in {"color", "bar"}:
        color = merged.get("color") or {}
        rng = color.get("range")
        _require(isinstance(rng, (list, tuple)) and len(rng) == 2 and len(rng[0]) == 3 and len(rng[1]) == 3,
                 f"{where}.color.range 必须是 [[h,s,v],[h,s,v]]")
    if kind == "template":
        _require(isinstance(merged.get("template"), str) and merged["template"], f"{where}.template 必须是路径")
        _require(0 < float(merged.get("threshold", 0.8)) <= 1, f"{where}.threshold 必须在 (0,1]")
    return merged


def _validate_event(event: dict, index: int) -> dict:
    where = f"events[{index}]"
    _require(isinstance(event, dict), f"{where} 必须是映射")
    merged = _deep_merge(EVENT_DEFAULTS, event)
    name = merged.get("name")
    _require(isinstance(name, str) and name, f"{where}.name 必须是非空字符串")
    where = f"events[{name}]"
    when = merged.get("when")
    _require(isinstance(when, dict) and when, f"{where}.when 必须是非空映射")
    kinds = {"threshold", "delta", "bool", "appears"}
    _require(when.get("type") in kinds, f"{where}.when.type 必须是 {sorted(kinds)}")
    _require(isinstance(when.get("field"), str) and when["field"], f"{where}.when.field 必须是非空字符串")
    return merged


def load_game(path: Path | str, app: dict | None = None) -> dict:
    path = Path(path)
    raw = _read_yaml(path)
    app = app or {}
    base = raw.get("base_resolution") or app.get("base_resolution") or [1920, 1080]
    _require(isinstance(base, (list, tuple)) and len(base) == 2, "game.base_resolution 必须是 [宽, 高]")

    anchors = raw.get("anchors") or {}
    _require(isinstance(anchors, dict), "game.anchors 必须是映射")
    for name, anchor in anchors.items():
        _require(isinstance(anchor, dict), f"anchors.{name} 必须是映射")
        _require("area" in anchor, f"anchors.{name} 需要 area")
        anchor["_rect"] = Rect.from_list(anchor["area"], f"anchors.{name}.area")

    fields = [_validate_field(item, index) for index, item in enumerate(raw.get("text_fields") or [])]
    names = [field["name"] for field in fields]
    _require(len(names) == len(set(names)), "text_fields 的 name 不能重复")

    detectors = [_validate_detector(item, index) for index, item in enumerate(raw.get("cv_detectors") or [])]
    detector_names = [detector["name"] for detector in detectors]
    _require(len(detector_names) == len(set(detector_names)), "cv_detectors 的 name 不能重复")

    events = [_validate_event(item, index) for index, item in enumerate(raw.get("events") or [])]
    known = set(names) | set(detector_names)
    for event in events:
        field = event["when"]["field"]
        _require(field in known, f"events.{event['name']} 引用了未定义的字段或检测器: {field}")

    for field in fields:
        anchor = field.get("anchor")
        if anchor:
            _require(anchor in anchors, f"text_fields.{field['name']} 引用了未定义的锚点: {anchor}")

    return {
        "id": path.stem,
        "path": str(path.resolve()),
        "base_resolution": [int(base[0]), int(base[1])],
        "anchors": anchors,
        "text_fields": fields,
        "cv_detectors": detectors,
        "events": events,
        "notes": raw.get("notes", ""),
    }


def resolve_game_path(root: Path | str, game: str) -> Path:
    return Path(root) / GAME_ROOT / f"{game}.yaml"


def load_all(root: Path | str, app_path: Path | str | None = None) -> tuple[dict, dict]:
    root = Path(root)
    app_file = Path(app_path) if app_path else root / "configs" / "app.yaml"
    app = load_app(app_file)
    game = load_game(resolve_game_path(root, app["game"]), app)
    return app, game


def area_pixels(entry: dict, size: tuple[int, int], anchor_offset: tuple[float, float] = (0.0, 0.0)) -> tuple[int, int, int, int]:
    """Resolve a field/detector/anchor rectangle to absolute pixels."""
    dx = dy = 0.0
    if entry.get("anchor"):
        dx, dy = anchor_offset
        base = entry["_anchor_rect"]
        left = base.x + dx + float(entry["offset"][0])
        top = base.y + dy + float(entry["offset"][1])
        return Rect(left, top, entry["_rect"].w, entry["_rect"].h).pixels(size)
    return entry["_rect"].pixels(size, dx, dy)


def summary(app: dict, game: dict) -> dict:
    """Compact description used by CLI/debug output."""
    return {
        "app": Path(app.get("_path", "")).name,
        "game": game["id"],
        "base_resolution": game["base_resolution"],
        "anchors": list(game["anchors"]),
        "text_fields": [field["name"] for field in game["text_fields"]],
        "cv_detectors": [detector["name"] for detector in game["cv_detectors"]],
        "events": [event["name"] for event in game["events"]],
        "vision_fallback": bool(app["vision"].get("enabled")),
    }


def ensure_anchor_rects(game: dict) -> None:
    """Attach the anchor rectangle to fields that reference one."""
    for field in game["text_fields"]:
        name = field.get("anchor")
        field["_anchor_rect"] = game["anchors"][name]["_rect"] if name else None
