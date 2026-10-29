# phone/ — PC 采集 · HUD 数字 · APK

这条链路上 PC 只做两件事：采集画面、把 UI 数字读成数值。手机只做两件事：拉流、
推理 + 调 DeepSeek 显示气泡。**手机端不做 OCR，PC 端不显示任何 AI 界面。**

```
PC(Windows)                                     手机(iQOO Neo10 / 8 Gen 3)
 pc_capture.py                                    CompanionService
   mss 抓屏 5FPS ──┐                               拉 /tick?since=n
   fields.yaml OCR ─┴─► GET /tick {seq, ts, image, ocr} ──► TFLite 检测
                      GET /apk  ← 安装包下载               融合 → DeepSeek
                                                          → pet.html 气泡
```

## 1. PC 端启动

依赖已经装在 `vision/.venv`（opencv / mss / rapidocr_onnxruntime / pyyaml）。

```powershell
# 全屏采集，默认 5 FPS、下发宽 640
D:\WBSJ\AIPW\lol-companion\vision\.venv\Scripts\python.exe phone\pc_capture.py

# 只截英雄联盟窗口（窗口标题模糊匹配）
... phone\pc_capture.py --window-title 英雄联盟

# 换游戏档位：同时换掉 OCR 字段集，手机端会自动跟着换模型
... phone\pc_capture.py --profile valorant
... phone\pc_capture.py --profile delta

# 不跑 OCR，只发画面
... phone\pc_capture.py --no-ocr

# 用一张静态图自测整条链路（不需要开游戏）
... phone\pc_capture.py --source image --image vision\samples\synthetic-hud.png
```

启动后会打印手机该访问的地址：

```
/tick   http://192.168.1.10:8770/tick        <- 手机拉这个
/info   http://192.168.1.10:8770/info
/apk    http://192.168.1.10:8770/apk
```

> 第一次运行 Windows 会弹防火墙授权，必须允许**专用网络**，否则手机连不上。

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8770 | 监听端口 |
| `--fps` | 5 | 抓屏频率；OCR 由 `fields.yaml` 的 `every_ms` 单独控制 |
| `--width` | 640 | 下发画面宽度，`0` 表示原尺寸 |
| `--quality` | 70 | JPEG 质量 |
| `--window-title` | 空 | 只截标题包含该关键字的窗口 |
| `--fields` | `phone/fields.yaml` | 字段定义 |
| `--profile` | 空 | 游戏档位，读 `phone/profiles/<name>.yaml` |
| `--no-ocr` | 关 | 跳过 OCR，`ocr.values` 为空 |

### 游戏档位（profile）

一个服务、一个 APK，靠档位名对齐两端的配置：

| 档位 | PC 端字段集 | 手机端模型文件 | 读什么 |
|---|---|---|---|
| `lol`（默认） | `phone/fields.yaml` | `lol.tflite` | 时钟、双方团队击杀、等级 |
| `valorant` | `phone/profiles/valorant.yaml` | `valorant.tflite` | 血量、护甲、弹药、经济、回合分 |
| `delta` | `phone/profiles/delta.yaml` | `delta.tflite` | 血量、弹药、倒计时、存活队友 |

规则有三条：

1. **档位文件是完整替换，不与 `fields.yaml` 合并**——否则打瓦罗兰特时还会去读 LoL 的等级框。
2. `/tick` 里带 `"profile": "valorant"`，手机在「自动跟随电脑」模式下据此加载 `valorant.tflite`，**不用手点两次**。
3. 手机同一时刻只加载一个模型，切换时先 `close()` 再建（GPU delegate 多实例会抢资源）。

`/apk`、`/info`、`/fields` 都会回报当前档位，`--profile` 写错时会提示并回退到 `fields.yaml`。

## 2. 协议

```
GET /tick?since=<seq>
```

```json
{
  "seq": 1287,
  "ts": 1761650000.123,
  "size": [1920, 1080],
  "image": "<base64 jpeg>",
  "ocr": {
    "values": {"timer": 1421, "allyKills": 15, "enemyKills": 8, "level": 11, "score": [15, 8]},
    "unstable": [],
    "calibrated": false,
    "ms": 13.9,
    "ageMs": 130
  }
}
```

- `since` 等于当前 `seq` → 只回 `{"unchanged": true, "seq": n}`（约 20 字节），手机跳过推理。
- `?image=0` → 不带图，手机在没有模型时用它省流量。
- `?debug=1` → 额外返回每字段的识别原文与置信度，校准的时候用。
- `unstable`：字段读不到、或读数已经超过 `stale_ms` 没更新。仍然给值，但标注不可靠。
- `calibrated`：ROI 是否在真实画面上校准过。现在是 `false`。

其它端点：`/frame`（原始 JPEG）、`/info`（计数与当前值）、`/fields`（当前字段配置）、
`/apk`（`phone/dist` 里最新的 APK）、`/apk/info`。

## 3. 手机端

### 拿 APK

本地不打包。到仓库的 **Releases** 里下载最新 `lolcompanion-pet-*.apk`
（`.github/workflows/android.yml` 在 GitHub Actions 上构建，产物挂在 Release，不用登录就能下）。

或者服务起了、`phone/dist` 里有 APK 时，手机浏览器直接开 `http://<PC_IP>:8770/apk`。

### 安装后三步

1. 点右上角 **设置** → 填电脑地址，例如 `192.168.1.10:8770`（不用写 `http://`）
2. 填 DeepSeek API Key（只存本机 SharedPreferences，**不写进 APK**）
3. 点 **开始**，允许通知权限；手机保持亮屏，与电脑同一个 Wi-Fi

顶部状态从「等待连接」变成「已连接」即通。断开时显示「连接断开，重试中…」，不会崩。

### 模型热换

按档位分文件放，文件名与 PC 的 `--profile` 同名：

```
/Android/data/com.lolcompanion.pet/files/models/valorant.tflite
/Android/data/com.lolcompanion.pet/files/models/valorant.labels.txt
/Android/data/com.lolcompanion.pet/files/models/delta.tflite
/Android/data/com.lolcompanion.pet/files/models/lol.tflite
/Android/data/com.lolcompanion.pet/files/models/current.tflite   ← 通用兜底
/Android/data/com.lolcompanion.pet/files/models/labels.txt
```

**`labels.txt` 的行顺序必须跟训练时的类别 id 一致**，错一格就会把队友说成敌人。
例如 keremberke 的瓦罗兰特模型是：`dropped spike / enemy / planted spike / teammate`。

应用内「设置 → 换模型」写入的是**当前档位**的文件名；「游戏档位」可选
自动跟随电脑 / 瓦罗兰特 / 三角洲 / 英雄联盟 / 仅对话。没有模型的档位降级为
**只调 DeepSeek、无检测**，气泡照常出来。

## 4. 校准 fields.yaml（重要）

`phone/fields.yaml` 里的矩形是按 16:9 常见 HUD 位置估的**占位值**，没在真实画面上校准过。
所以 `calibrated: false` 会一路传到手机，提示数字不可全信。

校准流程：

```powershell
# 1. 游戏里截一张全屏图（Win+Shift+S 存成 png）
# 2. 跑校准，看每个字段读到什么
... phone\calibrate.py --shot shots\hud.png

# 3. 它会写出 phone/calibration/<字段>.png 和 overlay.png
#    矩形画在 overlay 上，读到什么、置信度多少，一目了然
```

输出示例：

```
字段          读取              置信     解析        矩形
timer       23:41           0.83   1421      [0.427, 0.000, 0.145, 0.048]  279x52px
allyKills   15              0.67   15        [0.335, 0.005, 0.066, 0.038]  127x41px
enemyKills  8               0.35   8         [0.599, 0.005, 0.066, 0.038]  127x41px
level       11              0.62   11        [0.372, 0.905, 0.028, 0.040]  54x43px
```

读不出的字段就改它的 `rect`（归一化坐标，原点左上角），重复到全部可读，
再把 `fields.yaml` 顶部的 `calibrated` 改成 `true`。

字段支持：`kind`（int / float / pair / clock / text）、可选 `regex`（命名组 `a`/`b`/`v`/`m`+`s`）、
`min`/`max` 边界、`scale` 换算、`min_score` 单独阈值、`enabled` 开关、`derive` 派生成对字段。

**单字符字段（如敌方击杀「8」）置信度天然只有 0.35–0.50**，所以 `ocr.min_score` 默认 0.30。
靠 `min`/`max` 兜误读，不要靠提高阈值硬卡。

## 5. 桌面验证版 runner

APK 还没出来之前，用它验证「拉流 → 检测 → DeepSeek → 气泡文字」整条链：

```powershell
# 干跑：只打印要送给模型的 JSON
$env:DEEPSEEK_API_KEY="sk-..."
... phone\desktop_runner.py --host 192.168.1.10:8770 --dry-run --once

# 正常跑，控制台打印气泡
... phone\desktop_runner.py --host 192.168.1.10:8770

# 自检后处理逻辑（不需要模型、不需要网络）
... phone\desktop_runner.py --selftest
```

没有 `DEEPSEEK_API_KEY` 时不会报错，走本地兜底文案；没有模型时降级为纯 OCR。

送给模型的内容只有这些，**不带原始帧**：

```json
{
  "scene": {"objects": [{"cls": "person", "count": 2, "region": "center"}]},
  "hud": {"values": {"timer": 1421, "allyKills": 15}, "unstable": [], "calibrated": false},
  "events": [{"kind": "clock", "value": 1426}],
  "ask": "看一眼现在该干嘛，一句话。"
}
```

## 6. 模型

第一版用官方 COCO 权重验证链路，再换自训/现成模型。**不要手写 `yolo export`**——Windows 上
ultralytics 8.4 的 tflite 导出会直接报「只支持 Linux x86 和 macOS」，而且 `half=True` 已废弃、
`int8` 会让输入变 uint8（本项目用不了）。用带自检的脚本：

```powershell
... phone\train\export_tflite.py --model phone\models\valorant-yolov8s.pt --name valorant
... phone\train\export_tflite.py --check phone\models\valorant.tflite --name valorant
... phone\train\verify_parity.py --onnx phone\models\valorant-yolov8s.onnx --tflite phone\models\valorant.tflite
```

脚本会在 Windows 上自动回退成「ONNX → onnx2tf」，并在导出后检查：输入是否 float32、
输出是否 `1×(4+类别数)×8400`、`labels.txt` 行数是否与类别数一致。

`yolov8n.pt`（COCO，80 类）换过来的话，产物约 12MB（fp32），改名 `current.tflite` 即可。
后处理是置信度过滤 + NMS，`Detector.kt` 按输出张量形状自适应类别数，84 类、4 类、2 类都能吃。
详见 `phone/models/README.md`。

## 7. 常见问题

| 现象 | 原因 |
|---|---|
| 手机一直「等待连接」 | 电脑防火墙没放行 8770；或填的 IP 不是 `--info` 打印的那个 |
| `/tick` 有 image 但 `values` 为空 | OCR 读不到：跑 `calibrate.py` 看 rect 是否偏了；或游戏 HUD 没数字 |
| `enemyKills` 时有时无 | 单字符置信度在阈值边缘，属正常；`unstable` 会标出来 |
| 手机亮屏才动 | 浏览器/WebView 后台会被冻结，这是系统行为；APK 用前台服务 + 唤醒锁缓解 |
| 模型加载失败 | 目录里放的不是 float32 tflite，或 `labels.txt` 与类别数不一致 |
