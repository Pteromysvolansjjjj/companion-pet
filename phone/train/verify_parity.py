"""对比 ONNX 与 TFLite 的推理结果，确认转换没有把布局搞错。

    python phone/train/verify_parity.py \
        --onnx phone/models/valorant-yolov8s.onnx \
        --tflite phone/models/valorant.tflite \
        --image vision/samples/synthetic-hud.png

为什么需要这一步：onnx2tf 会把 NCHW 转成 NHWC，还会重排算子。形状对得上但数值全错
是最难查的一类问题——手机端只会表现为「什么都检测不到」，看不出是模型坏了。

判据：同一张图分别喂 ONNX（NCHW）和 TFLite（NHWC），逐元素比。YOLO 检测头输出是
连续值，fp32 转换后应有很高的一致性；最大绝对误差超过 0.05 就说明转换有问题。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

SIZE = 640
PAD = 114


def letterbox(path: Path, size: int = SIZE) -> tuple[np.ndarray, np.ndarray]:
    """返回 (NCHW float32 RGB, NHWC float32 RGB)，两者像素完全一致。"""
    image = cv2.imread(str(path))
    if image is None:
        raise SystemExit(f"读不到图片 {path}")
    height, width = image.shape[:2]
    scale = min(size / width, size / height)
    new_w, new_h = int(round(width * scale)), int(round(height * scale))
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), PAD, dtype=np.uint8)
    top, left = (size - new_h) // 2, (size - new_w) // 2
    canvas[top:top + new_h, left:left + new_w] = resized
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.ascontiguousarray(rgb.transpose(2, 0, 1)[None]), np.ascontiguousarray(rgb[None])


def main() -> int:
    parser = argparse.ArgumentParser(description="ONNX / TFLite 数值一致性检查")
    parser.add_argument("--onnx", required=True)
    parser.add_argument("--tflite", required=True)
    parser.add_argument("--image", default=str(Path(__file__).resolve().parents[2] / "vision" / "samples" / "synthetic-hud.png"))
    parser.add_argument("--tolerance", type=float, default=0.05, help="最大绝对误差上限")
    args = parser.parse_args()

    nchw, nhwc = letterbox(Path(args.image))

    import onnxruntime as ort

    session = ort.InferenceSession(args.onnx, providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    onnx_out = session.run(None, {input_name: nchw})[0]

    import tensorflow as tf

    interpreter = tf.lite.Interpreter(model_path=args.tflite)
    interpreter.allocate_tensors()
    detail_in = interpreter.get_input_details()[0]
    detail_out = interpreter.get_output_details()[0]
    interpreter.set_tensor(detail_in["index"], nhwc)
    interpreter.invoke()
    tflite_out = interpreter.get_tensor(detail_out["index"])

    print(f"onnx   {onnx_out.shape} {onnx_out.dtype}  {Path(args.onnx).name}")
    print(f"tflite {tflite_out.shape} {tflite_out.dtype}  {Path(args.tflite).name}")

    if onnx_out.shape != tflite_out.shape:
        print(f"[FAIL] 形状不一致：{onnx_out.shape} vs {tflite_out.shape}")
        return 1

    diff = np.abs(onnx_out - tflite_out)
    max_diff, mean_diff = float(diff.max()), float(diff.mean())
    scale = float(np.abs(onnx_out).mean()) or 1.0
    print(f"最大绝对误差 {max_diff:.5f}   平均绝对误差 {mean_diff:.6f}   相对 {mean_diff / scale:.4%}")

    finite = bool(np.isfinite(tflite_out).all())
    print(f"tflite 输出全为有限值：{finite}")

    # 检测头前 4 个通道是框，后面是类别分数；只看类别分数有没有被压平
    scores = tflite_out[0][4:]
    score_max = float(scores.max())
    print(f"类别分数最大值 {score_max:.4f}（全 0 或全负数说明转换失败）")

    ok = finite and max_diff <= args.tolerance and score_max > 0.0
    print("\nPASS 转换一致，可以上手机" if ok else "\nFAIL 转换有问题，别急着装机")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
