"""End-to-end voice test without a microphone.

Feeds a locally synthesised utterance through the real VoiceService path
(trim -> job queue -> faster-whisper -> emitted event) so the only untested
piece is the physical microphone.
"""

from __future__ import annotations

import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vision"))

from lolvision.voice import VoiceConfig, VoiceService, clean_transcript, trim_silence  # noqa: E402

WAV = ROOT / "logs" / "voice" / "sample-1.wav"
MODEL = ROOT / "vision" / "models" / "faster-whisper-small"
EXPECT = "打野小心一点"


def load_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
        channels = handle.getnchannels()
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        target = int(audio.size * 16000 / rate)
        audio = np.interp(np.linspace(0, 1, target), np.linspace(0, 1, audio.size), audio).astype(np.float32)
    return np.ascontiguousarray(audio)


def main() -> int:
    if not WAV.exists():
        print("run vision/voice_test.py first (it synthesises the sample)")
        return 2

    events: list[dict] = []
    done = threading.Event()

    def emit(message: dict) -> None:
        events.append(message)
        if message.get("type") == "voice":
            done.set()

    service = VoiceService(VoiceConfig(model="small", modelPath=str(MODEL), language="zh"), emit)
    service.start()

    audio = trim_silence(load_16k(WAV), 16000)
    print(f"audio: {audio.size / 16000:.1f}s")
    # Simulate a completed push-to-talk recording.
    service._buffers = [audio]
    service._recording = True
    service._record_started = time.monotonic()
    service._stop_recording("test")

    if not done.wait(30):
        print("no voice event received")
        service.stop()
        return 1
    service.stop()

    result = next(event for event in events if event.get("type") == "voice")
    text = clean_transcript(result.get("text", ""))
    print("text:", text)
    print("latencyMs:", result.get("latencyMs"), "durationMs:", result.get("durationMs"))
    ok = EXPECT in text
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
