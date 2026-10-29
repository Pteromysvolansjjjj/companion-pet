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
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vision"))

try:
    from lolvision.console import fix_console_encoding

    fix_console_encoding()
except Exception:  # noqa: BLE001 - 编码修复失败也要能跑
    pass


def app_dir() -> Path:
    """exe 所在目录（打包后）；源码运行时是 phone/。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_model(explicit: str = "") -> Path | None:
    """按顺序找 faster-whisper 模型目录。

    默认用 **base**：实测同一句 3.7 秒中文，base 转写 636ms、small 要 1983ms，
    快 3.1 倍，代价只是偶尔一个同音字（贪->摊）。陪玩场景里延迟比字准更重要，
    想更准就加 --model 指向 small。
    """
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.exists() else None
    for name in ("faster-whisper-base", "faster-whisper-small"):
        for candidate in (app_dir() / "voice" / name,
                          ROOT / "vision" / "models" / name):
            if candidate.exists():
                return candidate
    return None


def record_seconds(seconds: float, device: int | None = None) -> "np.ndarray":
    """录一段音频（自检用）。"""
    import numpy as np
    import sounddevice as sd

    frames: list = []

    def callback(indata, _frames, _time_info, _status):
        frames.append(indata.copy())

    stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                            blocksize=1600, callback=callback, device=device)
    stream.start()
    time.sleep(seconds)
    stream.stop()
    stream.close()
    if not frames:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(frames).reshape(-1)


# 瓦罗兰特/三角洲这类 FPS 的常用词，给 Whisper 当提示，识别率比空提示高。
FPS_PROMPT = ("瓦罗兰特 三角洲行动 对局语音：包抄 架枪 拉枪线 进点 烟闪 残血 "
              "打残 补枪 救人 闪现 大招 技能 开枪 换弹 血量 护甲 经济 回防 "
              "对面 小地图 龙 推塔 补刀 走位 队友 敌人")


# Whisper 偶尔输出全角标点（U+FE50 小写逗号之类），转成半角
_FULLWIDTH = str.maketrans({
    "﹑": ",", "﹐": ",", "，": ",", "。": ".", "、": ",", "！": "!", "？": "?",
    "；": ";", "：": ":", "（": "(", "）": ")", "％": "%", "～": "~",
})


def clean_text(text: str) -> str:
    """全角标点转半角，顺手压掉多余空白。"""
    return re.sub(r"\s+", " ", (text or "").translate(_FULLWIDTH)).strip()


def send_to_pc(base: str, text: str, timeout: float = 4.0) -> bool:
    body = json.dumps({"text": text, "source": "voice"}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/say", data=body, method="POST",
        headers={"content-type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status == 200


def transcribe_audio(audio, model_path: Path, language: str) -> tuple[str, int]:
    """转写一段 16k 单声道 float32 音频。

    必须开 vad_filter：全程录音模式下音频里大量是静音，
    Whisper 会在静音上做幻觉（疯狂重复），实测 5 秒静音要转 37 秒，
    开了 VAD 之后直接跳过静音段。
    """
    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=4)
    started = time.monotonic()
    segments, info = model.transcribe(
        np.ascontiguousarray(audio), language=language or None, beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
        condition_on_previous_text=False, initial_prompt=FPS_PROMPT,
    )
    text = "".join(segment.text for segment in segments).strip()
    return text, int((time.monotonic() - started) * 1000)


def transcribe_wav(path: Path, model_path: Path, language: str) -> tuple[str, int]:
    """离线转写一个 wav（自测用：不碰麦克风也能验证整条链路）。"""
    import wave

    import numpy as np

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
    return transcribe_audio(audio, model_path, language)


def main() -> int:
    parser = argparse.ArgumentParser(description="PC 麦克风 -> 手机端陪玩")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770, help="PC 采集服务的端口")
    parser.add_argument("--hotkey", default="F2", help="按住说话的热键，如 F2 / Ctrl+Alt+F2")
    parser.add_argument("--mode", choices=["always", "stream", "push", "toggle"],
                        default="always",
                        help="always=能量VAD断句（默认，最省 CPU）；"
                             "stream=Silero VAD 断句（不切句、游戏音效不误触发，"
                             "CPU 略高）；push=按住热键说话；toggle=按一下再按一下停")
    parser.add_argument("--partial-every", type=int, default=500,
                        help="流式模式下多久尝试一次部分转写（毫秒）")
    parser.add_argument("--floor", type=float, default=0.010,
                        help="always 模式的音量下限，调大可以少被游戏音效触发")
    parser.add_argument("--factor", type=float, default=2.6,
                        help="always 模式相对噪声底的倍数，调大更不容易误触发")
    parser.add_argument("--min-chars", type=int, default=2,
                        help="识别结果少于此字数就丢弃")
    parser.add_argument("--silence-ms", type=int, default=None,
                        help="静音多久算一句话说完（流式默认 280，其它模式 450）")
    parser.add_argument("--model", default="", help="faster-whisper 模型目录（默认自动找）")
    parser.add_argument("--selftest", action="store_true",
                        help="录 5 秒转写一次，确认麦克风和模型都通")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--device", type=int, default=None, help="麦克风设备序号")
    parser.add_argument("--list-devices", action="store_true", help="列出录音设备后退出")
    parser.add_argument("--wav", default="", help="离线转写一个 wav 就退出（自测用）")
    args = parser.parse_args()

    from lolvision.voice import (
        AlwaysOnVoice, StreamingVoice, VoiceConfig, VoiceService, list_devices,
    )

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
    model_path = find_model(args.model)
    if model_path is None:
        print("找不到 faster-whisper 模型目录")
        print("  源码运行：应该在 vision/models/faster-whisper-small")
        print("  打包运行：应该在 exe 同级的 voice/faster-whisper-small")
        print("也可以用 --model 显式指定")
        return 2

    if args.selftest:
        print("麦克风自检：录 5 秒，说点什么…")
        try:
            audio = record_seconds(5.0, args.device)
        except Exception as error:  # noqa: BLE001
            print(f"  麦克风打不开：{error}")
            return 2
        if audio.size == 0:
            print("  没有采到任何音频")
            return 2
        import numpy as np

        rms = float(np.sqrt(np.mean(audio ** 2)))
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        print(f"  采到 {audio.size / 16000:.1f} 秒  RMS={rms:.4f}  峰值={peak:.3f}")
        if peak < 0.005:
            print("  电平几乎为零：检查麦克风静音开关、系统录音设备选错、或对着别的设备说话")
            return 1
        print(f"  模型 {model_path.name}，加载中（第一次约 40 秒）…")
        import numpy as np
        from faster_whisper import WhisperModel

        started = time.monotonic()
        model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=4)
        print(f"  模型加载完成，用时 {int(time.monotonic() - started)} 秒")
        started = time.monotonic()
        segments, _info = model.transcribe(
            np.ascontiguousarray(audio), language=args.language or None, beam_size=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
            condition_on_previous_text=False, initial_prompt=FPS_PROMPT,
        )
        text = "".join(segment.text for segment in segments).strip()
        latency = int((time.monotonic() - started) * 1000)
        print(f"  识别结果：{text!r}   [转写 {latency}ms]")
        if not text:
            print("  没听清。可能是声音太小、语速太快，或者词表不匹配。")
            return 1
        print("  麦克风与模型都正常。")
        return 0

    if args.wav:
        wav_path = Path(args.wav)
        if not wav_path.exists():
            print(f"找不到音频 {wav_path}")
            return 2
        print(f"识别 {wav_path.name} …")
        text, latency = transcribe_wav(wav_path, model_path, args.language)
        text = clean_text(text)
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

    # 流式模式已经有部分结果在手，静音判定可以更短，不用等 450ms 确认
    # 实测：中文句子内部（逗号处）的停顿就有 400ms+，静音阈值低于 500 会把
    # 一句话切成两段。所以流式也不能靠砍这个值换速度，两边都用 450。
    silence_ms = args.silence_ms if args.silence_ms is not None else 450

    config = VoiceConfig(
        enabled=True,
        hotkey=args.hotkey,
        mode=args.mode,
        modelPath=str(model_path),
        language=args.language,
        device=args.device,
        prompt=FPS_PROMPT,
        silenceMs=silence_ms,
        minMs=400,
        maxMs=12000,
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
        text = clean_text(event.get("text") or "")
        latency = event.get("latencyMs")
        if len(text) < max(1, args.min_chars):
            print(f"  （太短，丢弃：{text!r}）", flush=True)
            return
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

    if args.mode == "stream":
        service = StreamingVoice(config, emit)
        service.floor = args.floor
        service.factor = args.factor
        service.partial_every_ms = max(200, args.partial_every)
    elif args.mode == "always":
        service = AlwaysOnVoice(config, emit)
        service.floor = args.floor
        service.factor = args.factor
    else:
        service = VoiceService(config, emit)
    status = service.start()

    print("=" * 58)
    print("  PC 麦克风 -> 手机陪玩")
    print("=" * 58)
    if args.mode == "stream":
        print("  模式 Silero VAD 断句：不会把一句话从逗号处切断，游戏音效也不易误触发")
        print(f"       音量下限 {args.floor}  静音断句 {silence_ms}ms")
    elif args.mode == "always":
        print("  模式 全程录音：一说话就录，静下来自动断句，不用按任何键")
        print(f"       音量下限 {args.floor}  噪声倍数 {args.factor}  "
              f"静音断句 {silence_ms}ms")
    else:
        print(f"  热键 {args.hotkey}（{'按住说话' if args.mode == 'push' else '按一下开始/再按一下停'}）")
    print(f"  模型 {model_path.name}  本地 CPU 识别，不上传音频")
    print(f"  目标 {base}/say")
    if status.get("error"):
        if args.mode in ("always", "stream"):
            print(f"  [注意] {status['error']}：麦克风打不开，检查系统录音权限")
        else:
            print(f"  [注意] {status['error']}：热键可能已被别的程序占用，换 --hotkey 试试")
    print("  按 Ctrl-C 退出")
    print("=" * 58)

    if args.mode in ("always", "stream"):
        # 模型没加载完之前说话是没人理的，明确等它就绪再提示
        print("  正在加载语音模型…", flush=True)
        load_started = time.monotonic()
        if service.wait_ready(180):
            print(f"  模型就绪（{int(time.monotonic() - load_started)} 秒），"
                  f"现在就可以说话了", flush=True)
        else:
            print("  模型加载超时：第一句会多等一会儿", flush=True)
        # 打印一次噪声底，方便用户判断灵敏度
        info = service.status()
        print(f"  当前噪声底 {info.get('noise')}  触发门限 {info.get('gate')}"
              f"（嫌太灵敏就调大 --floor）", flush=True)

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
