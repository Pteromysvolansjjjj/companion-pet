"""把 PC 采集服务打包成单文件 exe（双击即可运行）。

    python phone/build_exe.py

产物是 ``phone/LolCompanionPet.exe``——**故意放在 phone/ 里**，因为 exe 会把
「自己所在目录」当作配置根目录：``pet.json``、``fields.yaml``、``profiles/``、
``models/``、``dist/`` 都从那里找。放到子目录里这些就全找不到了。

依赖装在 ``vision/.venv``（含 opencv / rapidocr / onnxruntime / mss / pyyaml）。
产物约 200MB：RapidOCR 的 onnx 模型、onnxruntime、opencv 都打进去了。
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


def main() -> int:
    if not PYTHON.exists():
        print(f"缺少 {PYTHON}，先跑 vision/setup.ps1")
        return 2

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("正在安装 PyInstaller…")
        subprocess.run([str(PYTHON), "-m", "pip", "install", "-q", "pyinstaller",
                        "--index-url", "https://pypi.org/simple"], check=True)

    WORK.mkdir(parents=True, exist_ok=True)
    command = [
        str(PYTHON), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--console",
        "--name", "LolCompanionPet",
        # 产物直接落到 phone/，让 exe 旁边的 fields.yaml / profiles / models / dist 生效
        "--distpath", str(HERE),
        "--workpath", str(WORK / "build"),
        "--specpath", str(WORK),
        "--paths", str(ROOT / "vision"),
        # RapidOCR 的 onnx 模型与字典是包内数据，必须显式收集
        "--collect-data", "rapidocr_onnxruntime",
        # 它的识别器/检测器是 importlib 动态导入的，静态分析看不到，必须整包收
        "--collect-submodules", "rapidocr_onnxruntime",
        "--collect-submodules", "onnxruntime",
        "--collect-binaries", "onnxruntime",
        # 动态导入，静态分析看不到
        "--hidden-import", "yaml",
        "--hidden-import", "lolvision",
        "--hidden-import", "lolvision.layout",
        "--hidden-import", "lolvision.ocr",
        "--hidden-import", "lolvision.capture",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        str(HERE / "pc_capture.py"),
    ]
    print("开始打包（首次约 2-5 分钟）…")
    result = subprocess.run(command)
    if result.returncode != 0:
        print("打包失败，看上面的日志")
        return result.returncode

    exe = HERE / "LolCompanionPet.exe"
    if not exe.exists():
        print("没找到产物")
        return 1

    # 配置模板：只在不存在时写，避免覆盖用户改过的档位
    settings = HERE / "pet.json"
    if not settings.exists():
        settings.write_text(
            '{\n'
            '  "_说明": "改完保存，重新打开 exe 生效。profile 可选 lol / valorant / delta",\n'
            '  "profile": "valorant",\n'
            '  "port": 8770,\n'
            '  "fps": 5,\n'
            '  "width": 640,\n'
            '  "quality": 70,\n'
            '  "windowTitle": ""\n'
            '}\n',
            encoding="utf-8")

    print(f"\n产物 {exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"配置 {settings}")
    print("双击 exe 即可启动；关掉窗口或 Ctrl-C 停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
