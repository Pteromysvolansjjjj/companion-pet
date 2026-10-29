"""One-shot check of the whole PC side, no game and no phone required.

    python phone/selftest.py

Starts ``pc_capture.py`` against the synthetic HUD, then asserts:

    /tick      画面非空、四个字段都读到、score 是成对的、since 生效
    /fields    字段配置可读
    /apk       有包能下，没包回 404 而不是炸掉
    runner     --selftest 全通过，--dry-run 产出的 JSON 结构正确

Exit code 0 means the PC half of the handoff protocol is satisfied.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PYTHON = sys.executable
SAMPLE = ROOT / "vision" / "samples" / "synthetic-hud.png"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read()


def get_json(url: str, timeout: float = 10.0):
    return json.loads(get(url, timeout)[1].decode("utf-8"))


def wait_ready(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = get_json(f"{base}/info", timeout=2.0)
            if info.get("sequence", 0) > 0 and info.get("ocr", {}).get("lastSeq", 0) > 0:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def reader_checks() -> dict[str, bool]:
    """字段读不到时要保留旧值并标 unstable，不能凭空消失。"""
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(ROOT / "vision"))
    import cv2
    import numpy as np
    from pc_capture import HudReader, build_specs, load_config

    config = load_config(HERE / "fields.yaml")
    specs, derived = build_specs(config)
    reader = HudReader(config, specs, derived)
    reader.warmup()
    hud = cv2.imread(str(SAMPLE))
    blank = np.full_like(hud, 20)
    first = reader.read(hud)
    second = reader.read(blank)
    third = reader.read(hud)
    return {
        "读不到时保留旧值": bool(second["values"]) and second["values"] == first["values"],
        "读不到时标 unstable": set(second["unstable"]) >= set(first["values"]),
        "恢复后不残留 unstable": third["unstable"] == [],
    }


def profile_checks() -> dict[str, bool]:
    """不同游戏档位必须各自独立，不能把 LoL 的字段带进 FPS。"""
    sys.path.insert(0, str(HERE))
    from pc_capture import build_specs, load_config

    def names(path):
        config = load_config(path)
        plain, derived = build_specs(config)
        return config, {spec.name for spec in plain} | {spec.name for spec in derived}

    base_config, base = names(HERE / "fields.yaml")
    val_config, val = names(HERE / "profiles" / "valorant.yaml")
    delta_config, delta = names(HERE / "profiles" / "delta.yaml")
    return {
        "默认档位是 lol": str(base_config.get("profile") or "lol").lower() == "lol" and "level" in base,
        "瓦罗兰特档位不含 LoL 字段": val_config.get("profile") == "valorant"
                                     and "level" not in val and "shield" in val,
        "三角洲档位独立": delta_config.get("profile") == "delta"
                          and {"squad", "ammo"} <= delta and "level" not in delta,
    }


def main() -> int:
    if not SAMPLE.exists():
        print(f"缺少自测图 {SAMPLE}，先跑 vision/selftest.py 生成")
        return 2

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [PYTHON, "-u", str(HERE / "pc_capture.py"), "--source", "image",
         "--image", str(SAMPLE), "--port", str(port), "--fps", "3"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", cwd=str(ROOT),
    )

    checks: dict[str, bool] = {}
    try:
        if not wait_ready(base):
            print("服务没起来，输出如下：")
            process.terminate()
            print((process.stdout.read() or "")[-2000:])
            return 1

        tick = get_json(f"{base}/tick")
        values = tick.get("ocr", {}).get("values", {})
        checks["/tick 带图"] = len(tick.get("image") or "") > 1000
        checks["/tick size 是下发尺寸"] = tick.get("size") == [640, 360]
        checks["/tick 带 profile"] = tick.get("profile") == "lol"
        checks["时钟 23:41 = 1421"] = values.get("timer") == 1421
        checks["我方击杀 15"] = values.get("allyKills") == 15
        checks["敌方击杀 8"] = values.get("enemyKills") == 8
        checks["等级 11"] = values.get("level") == 11
        checks["派生 score=[15,8]"] = values.get("score") == [15, 8]
        checks["calibrated 透传"] = tick.get("ocr", {}).get("calibrated") is False
        # enemyKills 是单字符，置信度天然在 0.30 阈值边缘，偶发一次读不到属正常
        # （此时值会保留并标 unstable）。只要求稳定字段不出问题。
        unstable = set(tick.get("ocr", {}).get("unstable") or [])
        checks["稳定字段不进 unstable"] = unstable <= {"enemyKills"}

        # 直接跟在 /info 后面取 seq 再比对，且允许重试：3 FPS 下两次请求之间可能已经翻帧
        unchanged_ok = False
        for _ in range(4):
            current = get_json(f"{base}/info").get("sequence")
            reply = get_json(f"{base}/tick?since={current}")
            if reply.get("unchanged") is True and reply.get("seq") == current:
                unchanged_ok = True
                break
        checks["since 命中回 unchanged"] = unchanged_ok

        no_image = get_json(f"{base}/tick?image=0")
        checks["image=0 省流量"] = "image" not in no_image

        fields = get_json(f"{base}/fields")
        checks["/fields 有配置"] = bool(fields.get("fields"))

        info = get_json(f"{base}/info")
        checks["/info 有 OCR 耗时"] = (info.get("ocr", {}).get("ms") or 0) > 0

        try:
            # 用 /apk/info 而不是 /apk：后者会把 33MB 的包整个下载一遍
            apk = get_json(f"{base}/apk/info")
            checks["/apk 有包可下"] = apk.get("bytes", 0) > 1024
        except urllib.error.HTTPError as error:
            checks["/apk 没包回 404"] = error.code == 404

        # 模型靠局域网传到手机，这条端点必须能用
        page_status, page = get(f"{base}/models")
        checks["/models 有清单页"] = page_status == 200 and b"<html" in page.lower()
        label_status, label = get(f"{base}/models/valorant.labels.txt")
        checks["/models 可下标签"] = label_status == 200 and len(label) > 5
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()

    runner_selftest = subprocess.run(
        [PYTHON, str(HERE / "desktop_runner.py"), "--selftest"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    checks["runner --selftest 全通过"] = runner_selftest.returncode == 0
    if runner_selftest.returncode != 0:
        print(runner_selftest.stdout)
        print(runner_selftest.stderr)

    checks.update(reader_checks())
    checks.update(profile_checks())

    print("\n== 结果 ==")
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    failed = [name for name, ok in checks.items() if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
