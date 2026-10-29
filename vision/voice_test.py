"""Voice self-test: synthesise Chinese speech locally, then transcribe it.

Verifies that the Whisper model loads, that Chinese recognition works and how
long a 3-4 second utterance takes on this CPU.  No microphone is needed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "vision" / "models" / "faster-whisper-small"
OUT = ROOT / "logs" / "voice"

SENTENCES = [
    "打野小心一点，对面中单不见了。",
    "我这波要回城买装备，你先守一下塔。",
    "小龙还有三十秒刷新，我们集合。",
]


def synth(text: str, path: Path) -> bool:
    try:
        import win32com.client as win32
    except Exception:
        return False
    voice = win32.Dispatch("SAPI.SpVoice")
    for item in voice.GetVoices():
        if "Chinese" in item.GetDescription():
            voice.Voice = item
            break
    stream = win32.Dispatch("SAPI.SpFileStream")
    stream.Open(str(path), 3)
    voice.AudioOutputStream = stream
    voice.Speak(text)
    stream.Close()
    return True


def load_wav(path: Path):
    """Read a PCM wav into float32 mono, independent of PyAV version drift."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        channels = handle.getnchannels()
        width = handle.getsampwidth()
    if width == 2:
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        audio = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        audio = np.frombuffer(frames, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(audio)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if not MODEL_DIR.exists():
        print(f"model missing: {MODEL_DIR}")
        return 2

    from faster_whisper import WhisperModel

    started = time.perf_counter()
    model = WhisperModel(str(MODEL_DIR), device="cpu", compute_type="int8", cpu_threads=4)
    print(f"model load: {time.perf_counter() - started:.2f}s")

    ok = True
    for index, sentence in enumerate(SENTENCES, 1):
        wav = OUT / f"sample-{index}.wav"
        if not synth(sentence, wav):
            print("SAPI voice unavailable; skipping synthesis")
            return 3
        audio = load_wav(wav)
        began = time.perf_counter()
        segments, info = model.transcribe(
            audio, language="zh", beam_size=1, vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt="英雄联盟对局语音：上单 打野 中单 下路 辅助 小龙 大龙 团战 补刀",
        )
        text = "".join(segment.text for segment in segments).strip()
        elapsed = time.perf_counter() - began
        audio_len = len(audio) / 16000
        print(f"[{index}] {audio_len:.1f}s audio -> {elapsed:.2f}s | {text}")
        if not text:
            ok = False
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
