# phone/models/

模型文件不入库（`.gitignore` 里有 `*.pt` / `*.onnx` / `*.tflite`），这里放说明、标签文件和导出脚本的产物。

## 命名约定

PC 端推理用 `.onnx`（`desktop_runner.py --model`），手机端用 `.tflite`，文件名跟档位同名：

| 档位 | PC（可选） | 手机 |
| --- | --- | --- |
| `lol` | `lol.onnx` | `lol.tflite` + `lol.labels.txt` |
| `valorant` | `valorant.onnx` | `valorant.tflite` + `valorant.labels.txt` |
| `delta` | `delta.onnx` | `delta.tflite` + `delta.labels.txt` |
| 通用兜底 | `yolov8n.onnx` | `current.tflite` + `labels.txt` |

手机端的 `.tflite` 与 `.labels.txt` 要拷到
`/Android/data/com.lolcompanion.pet/files/models/` 下。

## 当前已有

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `valorant-yolov8s.pt` | 22.5 MB | Hugging Face `keremberke/yolov8s-valorant-detection`，4 类 |
| `valorant-yolov8s.onnx` | 42.7 MB | 上者的 ONNX 导出，转换中间产物 |
| `valorant.tflite` | 42.7 MB | **手机用这个**，float32 |
| `valorant.labels.txt` | — | 4 行，顺序来自权重本身 |
| `valorant_saved_model/` | — | onnx2tf 的中间输出，可删 |

**不要用里面的 `*_float16.tflite`。** 它只有 22MB，但 TFLite 的 CPU 内核不支持 fp16 卷积
（实测连 `allocate_tensors()` 都失败），只有 GPU delegate 能跑；手机端 GPU 一旦没起来就直接废。
手机上用 `valorant.tflite`（float32）。

`valorant.tflite` 已实测：

- 输入 `[1, 640, 640, 3]` **float32**，输出 `[1, 8, 8400]`（4 框 + 4 类）
- 与 ONNX 逐元素比对，最大绝对误差 **0.0013**、相对误差 0.0000%（转换没走样）
- 拿数据集里 3 张真实截图跑：`enemy100251…` → enemy 0.35，两张 `yoru…` → teammate 0.72 / 0.70（类别与文件名对得上）

⚠️ 敌方那类置信度偏低（0.35），手机端 `Detector` 默认 `conf=0.35` 会卡在边缘，实战建议调到 0.30。

## 标签文件必须跟模型同源

`*.labels.txt` 的行顺序就是类别 id，**错一格会把队友说成敌人**。不要凭印象写，从权重里 dump：

```bash
python -c "from ultralytics import YOLO; print(YOLO('valorant-yolov8s.pt').names)"
```

导出脚本会自动写这一份，不用手抄。

## 导出到手机

```bash
# 一次装好依赖（独立 venv，约 1GB，不动采集环境）
python -m venv phone/train/.venv
phone/train/.venv/Scripts/python.exe -m pip install ultralytics tensorflow onnx2tf

# 导出 + 自检 + 自动写 labels.txt
phone/train/.venv/Scripts/python.exe phone/train/export_tflite.py \
    --model phone/models/valorant-yolov8s.pt --name valorant

# 只检查一个已有的 tflite
phone/train/.venv/Scripts/python.exe phone/train/export_tflite.py \
    --check phone/models/valorant.tflite --name valorant

# 转换有没有走样（ONNX vs TFLite 逐元素）
phone/train/.venv/Scripts/python.exe phone/train/verify_parity.py \
    --onnx phone/models/valorant-yolov8s.onnx --tflite phone/models/valorant.tflite
```

### Windows 上的坑（已踩过）

ultralytics **8.4 起 LiteRT/TFLite 导出只支持 Linux x86 和 macOS**，Windows 上直接报
`LiteRT export only supported on Linux x86 and macOS`。`export_tflite.py` 会自动回退成
「先导 ONNX，再用 `onnx2tf` 转」——这条路在 Windows 上可用，产物与直连导出数值等价。

另外三条版本变更：

- `half=True` 已废弃，改叫 `quantize`；而 LiteRT **不支持 fp16**，只支持 `8`(int8)、`w8a16`、`w8a32`、`32`(fp32)。
- **必须用浮点**。`int8=True` 会让输入变成 uint8，`Detector.kt` 只吃 float32，会直接报「需要 float32」。
- **不要用 YOLO26 训**。它是 NMS-free，输出 `1×300×6`，本项目后处理是 `1×(4+nc)×8400`，接不上。用 `yolov8n.pt` 或 `yolo11n.pt`。
