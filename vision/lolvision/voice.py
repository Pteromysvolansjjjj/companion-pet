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


class VoiceService:
    def __init__(self, config: VoiceConfig, emit, log=None) -> None:
        self.config = config
        self.emit = emit
        self.log = log or (lambda *a: None)
        self.state = "idle"
        self._stream = None
        self._model = None
        self._model_lock = threading.Lock()
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

    def _transcribe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                audio, duration_ms, reason = self._job.get(timeout=0.2)
            except queue.Empty:
                continue
            started = time.monotonic()
            try:
                model = self._load_model()
                segments, info = model.transcribe(
                    audio, language=self.config.language or None, beam_size=1,
                    vad_filter=False, condition_on_previous_text=False,
                    initial_prompt="英雄联盟对局语音：上单 打野 中单 下路 辅助 小龙 大龙 团战 补刀 闪现 大招 打野来抓 守塔 推线",
                )
                text = "".join(segment.text for segment in segments).strip()
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
