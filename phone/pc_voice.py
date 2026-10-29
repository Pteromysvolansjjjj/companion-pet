"""PC 麦克风 -> 本地 Whisper -> 手机端陪玩。

和上游那套「语音进 DSH 会话」不同：这里不依赖 DSH，识别出的文字直接 POST 给
PC 采集服务的 ``/say``，手机 App 下一轮拉取时就会把这句话交给 DeepSeek，
桌宠在手机屏幕上回应。

用法（先用 PC 采集服务占着 8770）::

    vision\\.venv\\Scripts\\python.exe phone\\pc_voice.py

然后在游戏里按住 F2 说话，松手。识别结果会打印在这里，同时送到手机。

模型是本地的 ``vision/models/faster-whisper-small``（faster-whisper, int8, CPU），
不上传任何音频。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vision"))

DEFAULT_MODEL = ROOT / "vision" / "models" / "faster-whisper-small"

# 瓦罗兰特/三角洲这类 FPS 的常用词，给 Whisper 当提示，识别率比空提示高。
FPS_PROMPT = ("瓦罗兰特 三角洲行动 对局语音：包抄 架枪 拉枪线 进点 烟闪 残血 "
              "打残 补枪 救人 闪现 大招 技能 开枪 换弹 血量 护甲 经济 回防 "
              "对面 小地图 龙 推塔 补刀 走位 队友 敌人")


def send_to_pc(base: str, text: str, timeout: float = 4.0) -> bool:
    body = json.dumps({"text": text, "source": "voice"}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/say", data=body, method="POST",
        headers={"content-type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status == 200


def transcribe_wav(path: Path, model_path: Path, language: str) -> tuple[str, int]:
    """离线转写一个 wav（自测用：不碰麦克风也能验证整条链路）。"""
    import wave

    import numpy as np
    from faster_whisper import WhisperModel

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"只支持 16 bit PCM，实际 {width * 8} bit")
    audio = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        target = int(len(audio) * 16000 / rate)
        audio = np.interp(
            np.linspace(0, len(audio) - 1, target),
            np.arange(len(audio)), audio).astype("float32")
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=4)
    started = time.monotonic()
    segments, info = model.transcribe(
        np.ascontiguousarray(audio), language=language or None, beam_size=1,
        vad_filter=False, condition_on_previous_text=False, initial_prompt=FPS_PROMPT,
    )
    text = "".join(segment.text for segment in segments).strip()
    return text, int((time.monotonic() - started) * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description="PC 麦克风 -> 手机端陪玩")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770, help="PC 采集服务的端口")
    parser.add_argument("--hotkey", default="F2", help="按住说话的热键，如 F2 / Ctrl+Alt+F2")
    parser.add_argument("--mode", choices=["push", "toggle"], default="push",
                        help="push=按住说话；toggle=按一下开始，再按一下停")
    parser.add_argument("--model", default=str(DEFAULT_MODEL), help="faster-whisper 模型目录")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--device", type=int, default=None, help="麦克风设备序号")
    parser.add_argument("--list-devices", action="store_true", help="列出录音设备后退出")
    parser.add_argument("--wav", default="", help="离线转写一个 wav 就退出（自测用）")
    args = parser.parse_args()

    from lolvision.voice import VoiceConfig, VoiceService, list_devices

    if args.list_devices:
        devices = list_devices()
        if not devices:
            print("没有找到录音设备（需要 PortAudio）")
            return 2
        for item in devices:
            mark = "*" if item.get("default") else " "
            print(f" {mark} [{item['index']}] {item['name']}  通道 {item['channels']}")
        return 0

    base = f"http://{args.host}:{args.port}"
    model_path = Path(args.model)
    if not model_path.exists():
        print(f"找不到语音模型 {model_path}")
        print("去 docs/VOICE.md 看模型下载方式，或改 --model 指向别的模型目录")
        return 2

    if args.wav:
        wav_path = Path(args.wav)
        if not wav_path.exists():
            print(f"找不到音频 {wav_path}")
            return 2
        print(f"识别 {wav_path.name} …")
        text, latency = transcribe_wav(wav_path, model_path, args.language)
        print(f"  你说：{text}   [{latency}ms]")
        if not text:
            return 1
        try:
            ok = send_to_pc(base, text)
            print(f"  -> 已送到手机（{base}/say）" if ok else "  -> 发送失败")
            return 0 if ok else 1
        except urllib.error.URLError as error:
            print(f"  -> 发送失败：{error}（PC 采集服务在跑吗？）")
            return 1

    config = VoiceConfig(
        enabled=True,
        hotkey=args.hotkey,
        mode=args.mode,
        modelPath=str(model_path),
        language=args.language,
        device=args.device,
        prompt=FPS_PROMPT,
    )

    pending: list[str] = []

    def emit(event: dict) -> None:
        kind = event.get("type")
        if kind == "voice:state":
            state = event.get("state")
            if state == "recording":
                print("  ● 录音中…", flush=True)
            elif state == "transcribing":
                print("  … 识别中", flush=True)
            return
        if kind == "error":
            print(f"  [语音错误] {event.get('message')}", flush=True)
            return
        if kind != "voice":
            return
        text = (event.get("text") or "").strip()
        latency = event.get("latencyMs")
        if not text:
            print("  （没听清）", flush=True)
            return
        print(f"  你说：{text}   [{latency}ms]", flush=True)
        pending.append(text)
        # PC 采集服务可能刚启动或正在重启，重试几次
        for attempt in range(3):
            try:
                if send_to_pc(base, text):
                    print(f"  -> 已送到手机（{base}/say）", flush=True)
                    return
            except urllib.error.URLError as error:
                if attempt == 2:
                    print(f"  -> 发送失败：{error}（PC 采集服务在跑吗？）", flush=True)
                    return
                time.sleep(0.6)
            except OSError as error:
                print(f"  -> 发送失败：{error}", flush=True)
                return

    service = VoiceService(config, emit)
    status = service.start()

    print("=" * 58)
    print("  PC 麦克风 -> 手机陪玩")
    print("=" * 58)
    print(f"  热键 {args.hotkey}（{'按住说话' if args.mode == 'push' else '按一下开始/再按一下停'}）")
    print(f"  模型 {model_path.name}  本地 CPU 识别，不上传音频")
    print(f"  目标 {base}/say")
    if status.get("error"):
        print(f"  [注意] {status['error']}：热键可能已被别的程序占用，换 --hotkey 试试")
    print("  按 Ctrl-C 退出")
    print("=" * 58)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
