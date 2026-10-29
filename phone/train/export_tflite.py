"""把一个 YOLO 权重导成手机能直接吃的 .tflite，并自检格式。

    python phone/train/export_tflite.py --model phone/models/valorant-yolov8s.pt --name valorant
    python phone/train/export_tflite.py --check phone/models/valorant.tflite --name valorant

导出之后立刻检查三件事，任何一条不过就会喊停：

1. 输入必须是 float32（`Detector.kt` 只吃 float32；int8 量化会让它直接报错）
2. 输出必须是 ``1 x (4+类别数) x N`` 这类 YOLOv8/YOLO11 布局（YOLO26 是 NMS-free，
   输出 ``1x300x6``，与本项目后处理不兼容）
3. 类别顺序写成 ``<name>.labels.txt``，直接从权重里 dump，不靠记忆

依赖装在独立 venv（``phone/train/.venv``），不污染采集环境：:

    python phone/train/export_tflite.py --model ... --name ... \
        --python phone/train/.venv/Scripts/python.exe
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHONE = HERE.parent


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PHONE.parent))
    except ValueError:
        return str(path)


def check_tflite(path: Path, name: str) -> bool:
    """读 .tflite 的输入输出签名，判断能不能直接上手机。"""
    try:
        import numpy as np
        import tensorflow as tf
    except Exception as error:  # noqa: BLE001
        print(f"[check] 装不了 tensorflow，跳过自检：{error}")
        return False

    interpreter = tf.lite.Interpreter(model_path=str(path))
    try:
        interpreter.allocate_tensors()
    except Exception as error:  # noqa: BLE001
        print(f"[FAIL] 这个 tflite 在 CPU 上加载不了：{error}")
        print("       常见原因是 fp16 权重——TFLite 的 CPU 内核不支持 fp16 卷积，")
        print("       只有 GPU delegate 能跑。手机端 GPU 起不来就直接废，所以用 float32。")
        return False
    input_detail = interpreter.get_input_details()[0]
    output_detail = interpreter.get_output_details()[0]
    in_shape = input_detail["shape"].tolist()
    out_shape = output_detail["shape"].tolist()
    in_dtype = input_detail["dtype"]
    out_dtype = output_detail["dtype"]

    print(f"[check] 文件      {path.name}  {path.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"[check] 输入      {in_shape}  {np.dtype(in_dtype).name}")
    print(f"[check] 输出      {out_shape}  {np.dtype(out_dtype).name}")

    ok = True
    if in_dtype != np.float32:
        print("[FAIL] 输入不是 float32。导出时别加 int8=True（会变成 uint8）")
        ok = False
    if len(in_shape) != 4 or in_shape[-1] != 3:
        print("[FAIL] 输入不是 1xHxWx3 的 NHWC 布局")
        ok = False

    # YOLOv8/11 检测头：1 x (4+nc) x anchors
    if len(out_shape) == 3 and out_shape[0] == 1:
        channels, anchors = out_shape[1], out_shape[2]
        if channels < 5:
            print("[FAIL] 输出通道数不合理，不像检测头")
            ok = False
        else:
            classes = channels - 4
            print(f"[check] 类别数    {classes}（输出通道 {channels} = 4 框 + {classes} 类）")
            expected = PHONE / "models" / f"{name}.labels.txt"
            if expected.exists():
                lines = [line for line in expected.read_text(encoding="utf-8").splitlines() if line.strip()]
                if len(lines) == classes:
                    print(f"[check] 标签文件  {expected.name}  {len(lines)} 行，与模型一致")
                else:
                    print(f"[FAIL] {expected.name} 有 {len(lines)} 行，模型有 {classes} 类，必须对齐")
                    ok = False
    elif len(out_shape) == 3 and out_shape[-1] == 6:
        print("[FAIL] 这是 YOLO26 / 端到端 NMS-free 的输出 (1xNx6)，")
        print("       本项目 Detector.kt 的后处理是 1x(4+nc)x8400，接不上。请用 yolov8n.pt 或 yolo11n.pt 重训")
        ok = False
    else:
        print(f"[warn] 输出形状 {out_shape} 不是常见的检测头布局，请人工确认")

    if ok:
        print("[check] 通过：可以直接放进手机的 models/ 目录")
    return ok


def export_via_onnx(model, model_path: Path, args, quantize: int) -> Path | None:
    """Windows 回退路径：ultralytics 只导 ONNX，剩下交给 onnx2tf。

    ultralytics 8.4 起 LiteRT 导出「只支持 Linux x86 和 macOS」，Windows 上会直接报
    ``LiteRT export only supported on Linux x86 and macOS``。ONNX 导出不受此限，
    而 onnx2tf 是纯 Python + TF，Windows 上能跑，两条路出来的 tflite 数值一致
    （实测最大绝对误差 0.0013）。
    """
    import subprocess

    executable = Path(sys.executable).parent / ("onnx2tf.exe" if sys.platform == "win32" else "onnx2tf")
    if not executable.exists():
        print(f"[export] 缺少 {executable.name}，先装：{sys.executable} -m pip install onnx2tf")
        return None

    print("[export] 步骤 1/2 导出 ONNX")
    onnx_path = Path(str(model.export(format="onnx", imgsz=args.imgsz, opset=12, simplify=True)))
    if not onnx_path.exists():
        print("[export] ONNX 导出失败")
        return None

    print(f"[export] 步骤 2/2 onnx2tf 转换 {onnx_path.name}")
    out_dir = Path(args.out) / f"{args.name}_saved_model"
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [str(executable), "-i", str(onnx_path), "-o", str(out_dir), "-osd"]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print((result.stdout or "")[-1500:])
        print((result.stderr or "")[-800:])
        return None

    if quantize == 8:
        print("[warn] 这条回退路径只产出 float32，int8 需要在 Linux 上用 ultralytics 直接导")
    candidates = sorted(out_dir.glob("*float32*.tflite")) or sorted(out_dir.glob("*.tflite"))
    if not candidates:
        print(f"[export] onnx2tf 没有产出 tflite，检查 {out_dir}")
        return None
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 tflite 并自检格式")
    parser.add_argument("--model", default=str(PHONE / "models" / "valorant-yolov8s.pt"))
    parser.add_argument("--name", default="valorant", help="产物前缀，即档位名")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--out", default=str(PHONE / "models"))
    parser.add_argument("--quantize", default="32", choices=["32", "int8"],
                        help="32=fp32（默认，手机端必需）；int8=体积小但输入变 uint8，本项目用不了")
    parser.add_argument("--check", default="", help="只检查已有的 .tflite，不导出")
    args = parser.parse_args()

    if args.check:
        return 0 if check_tflite(Path(args.check), args.name) else 1

    model_path = Path(args.model)
    if not model_path.exists():
        print(f"找不到权重 {model_path}")
        return 2

    from ultralytics import YOLO

    model = YOLO(str(model_path))
    names = model.names
    print(f"[model] {model_path.name}  类别 {len(names)}: "
          + ", ".join(f"{index}={label}" for index, label in sorted(names.items())))

    if args.quantize == "int8":
        print("[warn] int8 量化会让模型输入变成 uint8，Detector.kt 直接不支持。仅供体积对比用。")
        quantize = 8
    else:
        quantize = 32

    # 8.4.83 起 format='tflite' 被统一成 LiteRT；两代写法都试一遍
    produced_path: Path | None = None
    for fmt in ("litert", "tflite"):
        export_args = dict(format=fmt, imgsz=args.imgsz, quantize=quantize)
        print(f"[export] {fmt} imgsz={args.imgsz} quantize={quantize}")
        try:
            produced = model.export(**export_args)
        except (TypeError, AssertionError, ValueError) as error:
            print(f"[export] {fmt} 不可用：{error}")
            continue
        candidate = Path(str(produced))
        if candidate.exists() and candidate.suffix == ".tflite":
            produced_path = candidate
            break
        candidates = sorted(model_path.parent.glob(f"{model_path.stem}_saved_model/*.tflite"))
        if candidates:
            produced_path = candidates[0]
            break

    if produced_path is None:
        # Windows 上 ultralytics 8.4 只允许在 Linux/macOS 直接导 LiteRT，
        # 但 ONNX 导出是本机支持的，再用 onnx2tf 转 tflite 效果等价。
        print("[export] 直连导出不可用，改走 ONNX → onnx2tf")
        produced_path = export_via_onnx(model, model_path, args, quantize)
    if produced_path is None:
        print("导出没有产出 .tflite，请把上面的日志发我")
        return 1
    print(f"[export] 产物 {produced_path}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{args.name}.tflite"
    shutil.copyfile(produced_path, target)

    labels = out_dir / f"{args.name}.labels.txt"
    labels.write_text(
        "\n".join(str(names[index]) for index in sorted(names)) + "\n", encoding="utf-8"
    )
    print(f"[write]  {rel(target)}")
    print(f"[write]  {rel(labels)}（顺序来自权重本身，不用手抄）")

    ok = check_tflite(target, args.name)
    print("\n下一步：把这两个文件拷到手机上")
    print(f"  /Android/data/com.lolcompanion.pet/files/models/{args.name}.tflite")
    print(f"  /Android/data/com.lolcompanion.pet/files/models/{args.name}.labels.txt")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
