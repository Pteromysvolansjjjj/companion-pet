"""Push-to-talk voice input: global hotkey -> microphone -> local Whisper.

The hotkey is registered with ``RegisterHotKey``.  That is a normal Windows
hotkey registration, not a keyboard hook: the process is told when the
combination fires and never sees other keystrokes.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

SAMPLE_RATE = 16000
BLOCK = 1600  # 100 ms

DEFAULT_PROMPT = ("英雄联盟对局语音：上单 打野 中单 下路 辅助 小龙 大龙 团战 补刀 闪现 "
                  "大招 打野来抓 守塔 推线")


def _vk(name: str) -> int | None:
    name = name.strip().upper()
    if not name:
        return None
    if len(name) == 1 and (name.isalnum()):
        code = ord(name)
        if 0x30 <= code <= 0x5A:
            return code
    if name.startswith("F") and name[1:].isdigit():
        index = int(name[1:])
        if 1 <= index <= 24:
            return 0x70 + index - 1
    named = {
        "SPACE": 0x20, "TAB": 0x09, "CAPSLOCK": 0x14, "ESC": 0x1B, "ESCAPE": 0x1B,
        "ENTER": 0x0D, "`": 0xC0, "~": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB,
        "]": 0xDD, "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    }
    return named.get(name)


def parse_hotkey(spec: str) -> tuple[int, int]:
    """``Ctrl+Alt+F2`` -> (modifier mask, virtual key)."""
    mods = MOD_NOREPEAT
    key = None
    for part in str(spec or "").replace("-", "+").split("+"):
        token = part.strip().upper()
        if token in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif token == "ALT":
            mods |= MOD_ALT
        elif token == "SHIFT":
            mods |= MOD_SHIFT
        elif token in ("WIN", "META", "SUPER"):
            mods |= MOD_WIN
        elif token:
            key = _vk(token) if key is None else key
    if key is None:
        key = _vk("F2")
    return mods, key


@dataclass
class VoiceConfig:
    enabled: bool = True
    hotkey: str = "F2"
    mode: str = "push"          # push | toggle
    model: str = "small"        # tiny | base | small | medium
    modelPath: str = ""
    language: str = "zh"
    device: int | None = None
    minMs: int = 300
    maxMs: int = 15000
    silenceMs: int = 800
    echo: str = "bubble"        # bubble | tray | off
    prompt: str = ""            # 覆盖默认的 initial_prompt（按游戏换词表）


class VoiceService:
    def __init__(self, config: VoiceConfig, emit, log=None) -> None:
        self.config = config
        self.emit = emit
        self.log = log or (lambda *a: None)
        self.state = "idle"
        self._stream = None
        self._model = None
        self._model_lock = threading.Lock()
        self._stt_lock = threading.Lock()
        self._stop = threading.Event()
        self._record_thread = None
        self._hotkey_thread = None
        self._buffers: list[np.ndarray] = []
        self._recording = False
        self._record_started = 0.0
        self._last_voice_at = 0.0
        self._noise = 0.004
        self._hotkey_error: str | None = None
        self._job: queue.Queue = queue.Queue()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> dict:
        if not self.config.enabled:
            return {"enabled": False}
        self._stop.clear()
        self._transcribe_thread = threading.Thread(target=self._transcribe_loop, name="voice-stt", daemon=True)
        self._transcribe_thread.start()
        self._hotkey_thread = threading.Thread(target=self._hotkey_loop, name="voice-hotkey", daemon=True)
        self._hotkey_thread.start()
        threading.Thread(target=self._preload, name="voice-preload", daemon=True).start()
        return {"enabled": True, "hotkey": self.config.hotkey, "mode": self.config.mode,
                "model": self.config.model, "error": self._hotkey_error}

    def stop(self) -> None:
        self._stop.set()
        self._recording = False
        self._close_stream()
        # A replacement service registered in the same instant would collide with
        # this registration, so wait for the hotkey thread to release it.
        thread = self._hotkey_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._set_state("idle")

    # ----------------------------------------------------------------- state
    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        self.emit({"type": "voice:state", "state": state})

    def status(self) -> dict:
        return {"state": self.state, "hotkey": self.config.hotkey, "mode": self.config.mode,
                "model": self.config.model, "loaded": self._model is not None,
                "error": self._hotkey_error}

    # ---------------------------------------------------------------- hotkey
    def _hotkey_loop(self) -> None:
        mods, vk = parse_hotkey(self.config.hotkey)
        registered = False
        for attempt in range(3):
            if self._stop.is_set():
                return
            if user32.RegisterHotKey(None, 1, mods, vk):
                registered = True
                break
            # Another instance may still be releasing the same combination.
            time.sleep(0.15)
        if not registered:
            self._hotkey_error = "HOTKEY_REGISTER_FAILED"
            self.emit({"type": "error", "scope": "voice", "code": "hotkey_register_failed",
                       "message": f"无法注册热键 {self.config.hotkey}，可能已被其他程序占用。"})
            return
        try:
            msg = wintypes.MSG()
            while not self._stop.is_set():
                # Peek + sleep keeps the loop responsive to stop().
                got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
                if got:
                    if msg.message == WM_HOTKEY:
                        if self.config.mode == "toggle" and self._recording:
                            self._stop_recording("toggle")
                        else:
                            self._begin_recording()
                            if self.config.mode == "push":
                                threading.Thread(target=self._watch_release, args=(vk,), daemon=True).start()
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.01)
        finally:
            user32.UnregisterHotKey(None, 1)

    def _watch_release(self, vk: int) -> None:
        # WM_HOTKEY has no key-up notification; poll the physical key state.
        deadline = time.monotonic() + self.config.maxMs / 1000 + 2
        while time.monotonic() < deadline:
            if not (user32.GetAsyncKeyState(vk) & 0x8000):
                break
            time.sleep(0.03)
        self._stop_recording("release")

    # -------------------------------------------------------------- recording
    def _open_stream(self) -> bool:
        if self._stream is not None:
            return True
        try:
            import sounddevice as sd

            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                device=self.config.device, callback=self._on_audio,
            )
            self._stream.start()
            return True
        except Exception as error:
            self._stream = None
            self.emit({"type": "error", "scope": "voice", "code": "microphone_unavailable",
                       "message": f"无法打开麦克风：{error}"})
            return False

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _on_audio(self, indata, _frames, _time_info, _status) -> None:
        if not self._recording:
            # Track a slow noise floor only while idle.
            self._noise = 0.98 * self._noise + 0.02 * float(np.sqrt(np.mean(indata ** 2)) or 0.0)
            return
        block = indata[:, 0].copy()
        self._buffers.append(block)
        if float(np.sqrt(np.mean(block ** 2))) > max(0.006, self._noise * 3):
            self._last_voice_at = time.monotonic()

    def _begin_recording(self) -> None:
        if self._recording or not self._open_stream():
            return
        self._buffers = []
        self._recording = True
        self._record_started = time.monotonic()
        self._last_voice_at = self._record_started
        self._set_state("recording")
        self.emit({"type": "voice:state", "state": "recording", "startedAt": int(time.time() * 1000)})
        threading.Thread(target=self._auto_stop, daemon=True).start()

    def _auto_stop(self) -> None:
        while self._recording and not self._stop.is_set():
            time.sleep(0.1)
            elapsed = (time.monotonic() - self._record_started) * 1000
            if elapsed >= self.config.maxMs:
                self._stop_recording("max")
                return
            if self.config.mode == "toggle":
                silence = (time.monotonic() - self._last_voice_at) * 1000
                if elapsed > self.config.minMs and silence > self.config.silenceMs:
                    self._stop_recording("silence")
                    return

    def _stop_recording(self, reason: str) -> None:
        if not self._recording:
            return
        self._recording = False
        self._set_state("transcribing")
        buffers, self._buffers = self._buffers, []
        audio = np.concatenate(buffers) if buffers else np.zeros(0, dtype=np.float32)
        audio = trim_silence(audio, SAMPLE_RATE)
        duration_ms = int(len(audio) / SAMPLE_RATE * 1000)
        if duration_ms < self.config.minMs:
            self._set_state("idle")
            return
        self._job.put((audio, duration_ms, reason))

    # ------------------------------------------------------------ recognition
    def _preload(self) -> None:
        try:
            self._load_model()
        except Exception as error:
            self.emit({"type": "error", "scope": "voice", "code": "model_load_failed",
                       "message": f"语音模型加载失败：{error}"})

    def _load_model(self):
        with self._model_lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            path = self.config.modelPath or self.config.model
            self._model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=2)
            return self._model

    def _transcribe(self, audio):
        """同步转写一段音频，返回 ``(text, info)``。

        用 _stt_lock 串行化：faster-whisper 的 model 实例不是线程安全的，
        流式模式下「部分转写」和「最终转写」可能同时想用模型。
        """
        model = self._load_model()
        with self._stt_lock:
            segments, info = model.transcribe(
                audio, language=self.config.language or None, beam_size=1,
                # 必须开 VAD：常开录音里大量是静音，Whisper 会在静音上做幻觉
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
                condition_on_previous_text=False,
                initial_prompt=self.config.prompt or DEFAULT_PROMPT,
            )
            return "".join(segment.text for segment in segments).strip(), info

    def _transcribe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._job.get(timeout=0.2)
            except queue.Empty:
                continue
            # 四元组代表「已经有现成文本，不用再转」
            preset = None
            if len(item) == 4:
                audio, duration_ms, reason, preset = item
            else:
                audio, duration_ms, reason = item
            started = time.monotonic()
            if preset is not None:
                text = preset
                info = None
            else:
                try:
                    text, info = self._transcribe(audio)
                except Exception as error:
                    self.emit({"type": "error", "scope": "voice", "code": "transcribe_failed",
                               "message": f"语音识别失败：{error}"})
                    self._set_state("idle")
                    continue
            latency = int((time.monotonic() - started) * 1000)
            self._set_state("idle")
            cleaned = clean_transcript(text)
            if not cleaned:
                self.emit({"type": "voice", "text": "", "durationMs": duration_ms,
                           "latencyMs": latency, "reason": reason, "empty": True})
                continue
            self.emit({"type": "voice", "text": cleaned, "durationMs": duration_ms,
                       "latencyMs": latency, "reason": reason,
                       "language": getattr(info, "language", self.config.language)})


class AlwaysOnVoice(VoiceService):
    """全程录音版：不用按键，一说话就录，静下来自动断句。

    与按热键版的区别只在采集侧：这里没有热键线程，麦克风一直开着，
    用能量 VAD 判断「有人在说话」：

        噪声底 noise 用 EMA 跟踪，门限 gate = max(floor, noise * factor)
        静音 idle 时：noise <- 0.97 * noise + 0.03 * rms
        连续超过门限 -> 起音，开始缓冲
        静音超过 silenceMs -> 断句，丢给 Whisper

    识别与转写完全复用父类，所以模型、词表、延迟行为都一样。
    """

    def __init__(self, config: VoiceConfig, emit, log=None) -> None:
        super().__init__(config, emit, log)
        self.floor = 0.010        # 绝对音量下限，麦克风增益低时靠它兜底
        self.factor = 2.6         # 相对噪声底的倍数
        self.min_chars = 1        # 识别结果少于此字数就丢弃
        self.state = "listening"

    # ------------------------------------------------------------- lifecycle
    def start(self) -> dict:
        if not self.config.enabled:
            return {"enabled": False}
        self._stop.clear()
        self._transcribe_thread = threading.Thread(target=self._transcribe_loop,
                                                   name="voice-stt", daemon=True)
        self._transcribe_thread.start()
        threading.Thread(target=self._preload, name="voice-preload", daemon=True).start()
        if not self._open_stream():
            return {"enabled": False, "error": "microphone_unavailable"}
        self._set_state("listening")
        return {"enabled": True, "mode": "always", "model": self.config.model}

    def stop(self) -> None:
        self._stop.set()
        self._recording = False
        self._close_stream()
        self.state = "idle"
        self.emit({"type": "voice:state", "state": "idle"})

    # ----------------------------------------------------------------- state
    def _set_state(self, state: str) -> None:
        # 常开模式没有「空闲」，空闲就是「在听」
        if state == "idle":
            state = "listening"
        super()._set_state(state)

    def status(self) -> dict:
        data = super().status()
        data["mode"] = "always"
        data["noise"] = round(self._noise, 5)
        data["gate"] = round(max(self.floor, self._noise * self.factor), 5)
        return data

    def wait_ready(self, timeout: float = 180.0) -> bool:
        """等模型加载完。没就绪时说话不会有人理，先让用户知道要等。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._model is not None:
                return True
            time.sleep(0.2)
        return self._model is not None

    # -------------------------------------------------------------- capture
    def _on_audio(self, indata, _frames, _time_info, _status) -> None:
        block = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(block ** 2)))
        now = time.monotonic()
        gate = max(self.floor, self._noise * self.factor)

        if not self._recording:
            # 空闲时只跟踪噪声底
            self._noise = 0.97 * self._noise + 0.03 * rms
            if rms >= gate:
                self._begin(now)
                self._buffers.append(block)
            return

        self._buffers.append(block)
        if rms >= gate:
            self._last_voice_at = now
        elif (now - self._last_voice_at) * 1000 >= self.config.silenceMs:
            self._finish("silence")
            return
        if (now - self._record_started) * 1000 >= self.config.maxMs:
            self._finish("max")

    def _begin(self, now: float) -> None:
        self._recording = True
        self._buffers = []
        self._record_started = now
        self._last_voice_at = now
        self._set_state("recording")
        self.emit({"type": "voice:state", "state": "recording",
                   "startedAt": int(time.time() * 1000)})

    def _finish(self, reason: str) -> None:
        if not self._recording:
            return
        self._recording = False
        buffers, self._buffers = self._buffers, []
        audio = np.concatenate(buffers) if buffers else np.zeros(0, dtype=np.float32)
        # 判定长短要用「有声段净时长」：trim_silence 会在尾部补 12 帧静音，
        # 0.2 秒的咔哒声裁完能到 0.3 秒，混进完整句子里根本分不出来。
        speech_ms = self._speech_ms(audio)
        if speech_ms < self.config.minMs:
            self._set_state("listening")
            return
        # 送去识别前先裁掉首尾静音，Whisper 更准
        audio = trim_silence(audio, SAMPLE_RATE)
        duration_ms = int(len(audio) / SAMPLE_RATE * 1000)
        # 队列积压说明游戏音效在疯狂误触发：丢掉最旧的，别让真句子排不上队
        while self._job.qsize() >= 2:
            try:
                self._job.get_nowait()
                self.log("[voice] 转写积压，丢掉一条旧的")
            except queue.Empty:
                break
        self._set_state("transcribing")
        self._job.put((audio, duration_ms, reason))

    def _speech_ms(self, audio: np.ndarray) -> int:
        """这段音频里真正有声的毫秒数。"""
        if audio.size == 0:
            return 0
        frame = max(1, int(SAMPLE_RATE * 0.02))
        usable = audio[: audio.size - audio.size % frame]
        if usable.size == 0:
            return 0
        energy = np.sqrt(np.mean(usable.reshape(-1, frame) ** 2, axis=1))
        gate = max(self.floor * 0.6, float(np.max(energy)) * 0.25)
        voiced = int(np.count_nonzero(energy > gate))
        return int(voiced * frame / SAMPLE_RATE * 1000)


class StreamingVoice(AlwaysOnVoice):
    """用 Silero VAD 断句的常开录音（取代纯音量的能量 VAD）。

    为什么值得换：能量 VAD 只看音量，分不清「句子中间的停顿」和「说完了」。
    实测同一段中文「对面中路有人，别贪这个兵」——

        能量 VAD(450ms)  切成两段：「对面中路」+「别摊着的」
        Silero VAD       连续一段：0.00 - 3.18s

    被切句的后果不只是识别变差：缓冲区被重置，后面什么也接不上。

    关于「边说边转」：`_partial_loop` 保留了但**默认不启动**。实测转写 2.7 秒
    音频要 ~500ms，最新的部分结果永远落后 500-800ms，而断句阈值只有 450ms，
    复用的条件几乎不可能满足。等以后换真正的流式模型再启用。

    起音仍然用音量（便宜、灵敏度可用 --floor 调），只有断句换成 Silero。
    """

    def __init__(self, config: VoiceConfig, emit, log=None) -> None:
        super().__init__(config, emit, log)
        self.partial_every_ms = 500    # 保留字段，兼容旧调用
        self.partial_min_ms = 800
        self._partial_text = ""
        self._partial_ms = 0
        self._partial_thread = None
        self._silero_at = 0.0
        self._silero_ms = 200          # 多久用 Silero 判一次
        self._silero_ok = True         # Silero 可用性

    # ---------------------------------------------------------------- 判定
    def _said_done(self) -> bool:
        """整句话是不是已经说完了（用 Silero 判，不靠音量）。

        为什么不能只看音量：实测同一段中文「对面中路有人，别贪这个兵」，
        能量 VAD 在 450ms 阈值下把逗号停顿当成句尾，切成两段；
        Silero 对整段音频的输出是连续的一段 0.00-3.18s。

        代价：每次都要把从起音到现在的整段音频送进 Silero。3 秒音频约 10ms，
        每 200ms 跑一次，占用可接受，换来的是「不切句 + 游戏音效不误触发」。
        """
        if not self._silero_ok or not self._buffers:
            return False
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps
        except Exception:
            self._silero_ok = False
            return False

        audio = np.concatenate(self._buffers)
        if audio.size < int(SAMPLE_RATE * 0.6):
            return False
        try:
            stamps = get_speech_timestamps(
                audio, VadOptions(min_silence_duration_ms=450, speech_pad_ms=0))
        except Exception:
            self._silero_ok = False
            return False
        if not stamps:
            return True
        # 最后一段人声结束之后，又过了 450ms 没人说话
        return (audio.size - stamps[-1]["end"]) >= int(SAMPLE_RATE * 0.45)

    # ---------------------------------------------------------------- 采集
    def _begin(self, now: float) -> None:
        self._partial_text = ""
        self._partial_ms = 0
        self._silero_at = now
        super()._begin(now)

    def _on_audio(self, indata, _frames, _time_info, _status) -> None:
        block = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(block ** 2)))
        now = time.monotonic()
        gate = max(self.floor, self._noise * self.factor)

        if not self._recording:
            # 起音仍然看音量：便宜、灵敏，灵敏度还能用 --floor 调
            self._noise = 0.97 * self._noise + 0.03 * rms
            if rms >= gate:
                self._begin(now)
                self._buffers.append(block)
            return

        self._buffers.append(block)
        if rms >= gate:
            self._last_voice_at = now

        # 断句交给 Silero：每 200ms 看一眼整段音频里人声是不是已经断了
        if now - self._silero_at >= self._silero_ms / 1000.0:
            self._silero_at = now
            if self._said_done():
                self._finish("silence")
                return
        if (now - self._record_started) * 1000 >= self.config.maxMs:
            self._finish("max")

    def _partial_loop(self) -> None:
        """（保留但默认不启动）边说边转的部分结果。

        实测结论：不划算。转写 2.7 秒音频要 ~500ms，所以最新的部分结果
        永远比当前时刻落后 500-800ms，而断句阈值只有 450ms——追不上，
        复用的条件几乎不可能满足。留着代码是为了以后换更快的模型
        （或真正的流式模型）时能直接用。
        """
        while not self._stop.is_set():
            time.sleep(self.partial_every_ms / 1000.0)
            if not self._recording:
                return
            if self._model is None:
                continue
            snapshot = list(self._buffers)
            if not snapshot:
                continue
            audio = np.concatenate(snapshot)
            duration_ms = int(len(audio) / SAMPLE_RATE * 1000)
            if duration_ms < self.partial_min_ms:
                continue
            if duration_ms <= self._partial_ms + 250:
                continue
            try:
                text, _info = self._transcribe(np.ascontiguousarray(audio))
            except Exception:  # noqa: BLE001
                continue
            if text:
                self._partial_text = text
                self._partial_ms = duration_ms

    # ---------------------------------------------------------------- 断句
    def _finish(self, reason: str) -> None:
        if not self._recording:
            return
        self._recording = False
        buffers, self._buffers = self._buffers, []
        audio = np.concatenate(buffers) if buffers else np.zeros(0, dtype=np.float32)
        speech_ms = self._speech_ms(audio)
        if speech_ms < self.config.minMs:
            self._set_state("listening")
            return
        total_ms = int(len(audio) / SAMPLE_RATE * 1000)

        # 部分结果能覆盖到结尾就直接用（换更快模型时这条会命中）
        if self._partial_text and self._partial_ms >= total_ms - 450:
            self._set_state("transcribing")
            self._job.put((np.zeros(0, dtype=np.float32), total_ms, reason,
                           self._partial_text))
            return

        trimmed = trim_silence(audio, SAMPLE_RATE)
        duration_ms = int(len(trimmed) / SAMPLE_RATE * 1000)
        self._set_state("transcribing")
        self._job.put((trimmed, duration_ms, reason))

    def stop(self) -> None:
        self._recording = False
        super().stop()

    def status(self) -> dict:
        data = super().status()
        data["mode"] = "stream"
        data["partialText"] = self._partial_text
        data["partialMs"] = self._partial_ms
        return data


def trim_silence(audio: np.ndarray, rate: int, threshold: float = 0.006) -> np.ndarray:
    if audio.size == 0:
        return audio
    frame = max(1, int(rate * 0.02))
    usable = audio[: audio.size - audio.size % frame]
    if usable.size == 0:
        return audio
    energy = np.sqrt(np.mean(usable.reshape(-1, frame) ** 2, axis=1))
    voiced = np.where(energy > max(threshold, float(np.max(energy)) * 0.08))[0]
    if voiced.size == 0:
        return np.zeros(0, dtype=np.float32)
    start = max(0, int((voiced[0] - 8) * frame))
    end = min(audio.size, int((voiced[-1] + 12) * frame))
    return np.ascontiguousarray(audio[start:end])


def clean_transcript(text: str) -> str:
    """Drop Whisper artefacts that are not speech."""
    text = (text or "").strip()
    for filler in ("[BLANK_AUDIO]", "(silence)", "[silence]", "谢谢观看", "字幕由", "请不吝点赞"):
        if text.startswith(filler):
            return ""
    if text in ("。", ".", "，", ",", "…"):
        return ""
    return text


def list_devices() -> list[dict]:
    try:
        import sounddevice as sd

        devices = []
        for index, item in enumerate(sd.query_devices()):
            if item.get("max_input_channels", 0) > 0:
                devices.append({"index": index, "name": item.get("name"),
                                "channels": item.get("max_input_channels"),
                                "default": index == sd.default.device[0]})
        return devices
    except Exception:
        return []
