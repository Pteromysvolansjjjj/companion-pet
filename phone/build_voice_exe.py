"""把「PC 麦克风 -> 手机陪玩」打包成单文件 exe（双击即可运行）。

    python phone/build_voice_exe.py

产物 ``phone/LolCompanionVoice.exe``。

**模型不进 exe**：462MB 的 faster-whisper 放在 exe 同级的
``phone/voice/faster-whisper-small/``，打包脚本会从 ``vision/models/`` 拷过去。
这样 exe 本身只有一百多 MB，换模型也不用重新打包。

依赖（都在 ``vision/.venv``）：faster_whisper / ctranslate2 / sounddevice / tokenizers。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PYTHON = ROOT / "vision" / ".venv" / "Scripts" / "python.exe"
WORK = ROOT / "logs" / "pyinstaller"
MODEL_SRC = ROOT / "vision" / "models" / "faster-whisper-small"
MODEL_DST = HERE / "voice" / "faster-whisper-small"


def main() -> int:
    if not PYTHON.exists():
        print(f"缺少 {PYTHON}")
        return 2

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("正在安装 PyInstaller…")
        subprocess.run([str(PYTHON), "-m", "pip", "install", "-q", "pyinstaller",
                        "--index-url", "https://pypi.org/simple"], check=True)

    if not MODEL_SRC.exists():
        print(f"找不到模型 {MODEL_SRC}")
        print("去 docs/VOICE.md 看模型下载方式")
        return 2

    WORK.mkdir(parents=True, exist_ok=True)
    command = [
        str(PYTHON), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--console",
        "--name", "LolCompanionVoice",
        "--distpath", str(HERE),
        "--workpath", str(WORK / "voice-build"),
        "--specpath", str(WORK),
        "--paths", str(ROOT / "vision"),
        # faster-whisper 的推理内核是 ctranslate2，dll 必须在
        "--collect-all", "ctranslate2",
        "--collect-all", "faster_whisper",
        # tokenizer 的 Rust 扩展与 assets
        "--collect-all", "tokenizers",
        # PyAV：faster-whisper 用它解音频
        "--collect-all", "av",
        # PortAudio：sounddevice 靠它开麦克风
        "--collect-all", "sounddevice",
        # onnxruntime：faster-whisper 的 VAD 用得到
        "--collect-all", "onnxruntime",
        "--hidden-import", "lolvision",
        "--hidden-import", "lolvision.voice",
        "--hidden-import", "numpy",
        # 别把 torch 拖进来（几百 MB 且用不上）
        "--exclude-module", "torch",
        "--exclude-module", "torchvision",
        "--exclude-module", "matplotlib",
        "--exclude-module", "tkinter",
        str(HERE / "pc_voice.py"),
    ]
    print("开始打包语音 exe（约 2-4 分钟）…")
    result = subprocess.run(command)
    if result.returncode != 0:
        print("打包失败，看上面的日志")
        return result.returncode

    exe = HERE / "LolCompanionVoice.exe"
    if not exe.exists():
        print("没找到产物")
        return 1

    # 模型放到 exe 同级
    if MODEL_DST.exists():
        shutil.rmtree(MODEL_DST)
    MODEL_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(MODEL_SRC, MODEL_DST)
    size = sum(f.stat().st_size for f in MODEL_DST.rglob("*") if f.is_file())

    print(f"\n产物 {exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"模型 {MODEL_DST}  ({size / 1024 / 1024:.0f} MB)")
    print("\n用法：")
    print("  双击 exe            直接开始全程录音，说完自动送到手机")
    print("  加 --selftest 参数   录 5 秒转写一次，先验证麦克风通不通")
    print("  加 --port 8770      改 PC 采集服务的端口")
    print("\n模型别删：exe 会从同级 voice/faster-whisper-small 读。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
