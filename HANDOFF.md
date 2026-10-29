# 交接书：AI 陪玩（PC 采集 + 手机推理 + 云端 DeepSeek + 手机桌宠）

> 把本文件全文粘贴到新会话作为第一条消息即可继续。所有路径均为绝对路径。

## 进度更新（2026-10-28 夜）

第 12 节的 1-2 步已完成；Android 工程与工作流**只到源码，本地没有打包**（按用户要求，
本地不折腾 APK，等 GitHub Actions）。

| 产物 | 路径 | 验证情况 |
| --- | --- | --- |
| HUD 字段定义 | `phone/fields.yaml` | 占位 ROI，`calibrated: false` |
| `/tick` + OCR + `/apk` | `phone/pc_capture.py` | `python phone/selftest.py` → 15/15 |
| 桌面验证版 | `phone/desktop_runner.py` | `--selftest` 7/7；无 Key 走本地兜底 |
| 校准工具 | `phone/calibrate.py` | 合成画面 4/4 字段可读 |
| PC 侧自检 | `phone/selftest.py` | 15/15 |
| Android 工程 | `android/` | 源码完整，**未编译**（本机无 JDK/SDK） |
| 出包工作流 | `.github/workflows/android.yml` | YAML 校验通过，**未推送** |
| 使用说明 | `phone/README.md` | — |

实测（合成 HUD 1920x1080）：`timer=1421 allyKills=15 enemyKills=8 level=11 score=[15,8]`，
OCR 14-18ms，`/tick` 首包 8.9KB（静态图；真实画面约 40-50KB）。

关键实现选择（改代码前先看这三条）：

1. **OCR 前先紧致裁剪再放大到 56px 高**（`pc_capture.tighten/prepare`）。不做这一步，
   单字符「8」会被读成「¥８」且置信度只有 0.39；做了之后是「8」/0.35，多字符字段也涨点
   （clock 0.64→0.83）。因此 `ocr.min_score` 默认 **0.30**，靠字段 `min`/`max` 兜误读。
2. `/tick` 的 `size` 是**下发图尺寸**（640x360），采集分辨率在 `/info` 的 `sourceSize`。
3. `unstable` 的语义是「有值但不可靠」：读不出、或超过 `stale_ms` 没更新。字段不会被删掉。

### 游戏档位（profile，2026-10-29 上午）

一个服务、一个 APK，靠档位名对齐两端：

| 档位 | PC 字段集 | 手机模型文件 | 读什么 |
| --- | --- | --- | --- |
| `lol`（默认） | `phone/fields.yaml` | `lol.tflite` | 时钟 / 双方团队击杀 / 等级 |
| `valorant` | `phone/profiles/valorant.yaml` | `valorant.tflite` | 血量 / 护甲 / 弹药 / 经济 / 回合分 |
| `delta` | `phone/profiles/delta.yaml` | `delta.tflite` | 血量 / 弹药 / 倒计时 / 存活队友 |

- `--profile X` 读 `phone/profiles/X.yaml`。档位文件是**完整替换**，不与 `fields.yaml`
  合并——否则打瓦罗兰特时会去读 LoL 的等级框（这个 bug 已经踩过一次，自检里锁住了）。
- `/tick` 带 `profile`，手机在「自动跟随电脑」模式下据此自动换模型，不用手点两次。
- 手机同一时刻只保留一个 Interpreter，切换时先 `close()` 再建（GPU delegate 多实例会抢资源）。
- 自检 22 项，含 profile 透传与三档隔离。

模型调研结论（同日上午）：只有瓦罗兰特有现成权重
（`keremberke/yolov8s-valorant-detection`，4 类，mAP@0.5 自报 0.971，HF 页面**未标 license**）；
三角洲只有干净数据集（Ultralytics `dove-25/delta-force-mergedyolov8`，14,820 张 2 类，MIT）；
LoL 没有可用公开权重，且 640 宽下发时英雄只有 10–15px，建议改检小地图或直接用 LCU 接口。

### 瓦罗兰特模型已转好并实测（2026-10-29 上午）

`phone/models/` 下已有 `valorant.tflite`（42.7MB float32）+ `valorant.labels.txt`（4 行）。
实测：输入 `[1,640,640,3]` float32、输出 `[1,8,8400]`（4 框 + 4 类）；与 ONNX 逐元素比对
最大误差 0.0013；数据集里 3 张真实截图跑出 enemy 0.35 / teammate 0.72 / 0.70，类别与文件名对得上。
**敌方置信度偏低，手机端 conf 建议从 0.35 调到 0.30。**

三个必须记住的导出约束（都踩过了）：

1. **ultralytics 8.4 起 tflite 导出只支持 Linux x86 和 macOS**，Windows 直连必失败。
   `phone/train/export_tflite.py` 会自动回退成「ONNX → onnx2tf」，产物数值等价。
2. `half=True` 已废弃改叫 `quantize`，且 LiteRT 不支持 fp16；**必须 fp32**，int8 会让输入变 uint8，
   `Detector.kt` 直接拒绝。
3. **不要用 YOLO26 训**：它 NMS-free，输出 `1×300×6`，本项目后处理是 `1×(4+nc)×8400`，接不上。

训练环境是独立的 `phone/train/.venv`（约 1GB，已 gitignore），不污染采集用的 `vision/.venv`。

### 仓库与出包（2026-10-29 中午，已完成）

- 仓库：https://github.com/Pteromysvolansjjjj/companion-pet （public，`main`）
- 首次推送失败过一次，原因是**本地是浅克隆**（`.git/shallow` 边界在 `c26c300`），
  push 时需要发送的增量基对象在边界外，报 `did not receive expected object`。
  修复：`git fetch --unshallow upstream`。以后重新 clone 时若用 `--depth`，还会遇到同样问题。
- 第二次构建成功：run #2 全绿，Release `apk-2` 里的 `lolcompanion-pet-2.apk`（33.07 MB，
  四个 ABI 全打进去了），已下载到 `phone/dist/`，`/apk` 端点可直接下。
- 编译期修掉一个真实错误：`buildNotification` 调用传了 2 个参数、签名只有 1 个
  （`CompanionService.kt:165`），补上 `source` 参数并让通知栏在兜底时显示「（本地兜底）」。

> 想减小 APK：在 `android/app/build.gradle` 的 `defaultConfig` 里加
> `ndk { abiFilters 'arm64-v8a' }`，可砍掉另外三个 ABI（约省 15MB）。
> 8 Gen 3 只需 arm64。

明天要做的三件事（都需要人工）：

1. 推仓库 → 跑 Actions → 从 Release 下 APK（工作流已就绪，不需要本地 JDK）
2. 手机装 APK：填 `PC_IP:8770` + DeepSeek Key → 点开始
3. 真实画面校准：`python phone/calibrate.py --shot shot.png` → 按 overlay 改 rect → `calibrated: true`

未验证项（缺环境，不是代码问题）：YOLO 真实推理（`phone/models/yolov8n.onnx` 不存在，
只测了后处理逻辑）、Kotlin 编译、真机 WebView、DeepSeek 真实调用（无 Key）。

## 0. 一句话目标

玩家在电脑上玩单机/离线游戏；电脑只负责采集画面并用 OCR 读出 UI 数字，把「一帧图 + 一组数字」发给手机；手机用 YOLO 看懂画面，把结构化结果连同 OCR 一起发给云端 DeepSeek；DeepSeek 的回复显示在手机端的桌宠界面上。

## 1. 最终架构（已定稿，不要再改）

```
PC(Windows)                                         手机(iQOO Neo10 / 8 Gen 3 / 12GB)
 capture 5 FPS → 640 宽 JPEG                    ┌─ 拉 /tick（图 + OCR 数字）
 OCR 固定 ROI → 数字 + 校验                      │   YOLO(tflite) 推理 → 目标 + IoU 跟踪
 HTTP /tick: {seq, ts, image(base64), ocr}  ───►│   融合 → POST DeepSeek → 气泡
                                                └─ 桌宠页面（WebView）
```

分工硬约束：
- PC 端**不显示任何 AI 界面**，只输出数据。
- 手机端**不做 OCR**（太慢），OCR 固定在 PC。
- 云端只用 DeepSeek 做对话，不做视觉。

## 2. 适用范围

本项目只面向**单机/离线模式**（单机战役、靶场、自定义对 AI）：检测世界物体、保留坐标、任意推理都不受限制。

不做的事只有一件：**联机对局的实时敌人回显**。这条不是配置开关，是这套代码本身不实现的能力——不会出现在设置里，也不会通过参数打开。

## 3. 现有代码与状态

项目根：`D:\WBSJ\AIPW\lol-companion`（**所有产出只写这里，禁止写 C 盘**）
版本：`0.7.1-screen.1`（fork 自 jinghuashuiyue2004/dsh-plugin-lol-companion 0.7.1）

已完成且可用：

| 模块 | 路径 | 状态 |
| --- | --- | --- |
| 接口数据源（LCU + Live Client Data） | `src/game/*` | ✅ 原有能力，完整 |
| 屏幕 OCR 侧车 | `vision/lolvision/*`、`vision/main.py` | ✅ RapidOCR 离线，合成画面自测通过 |
| 语音输入 | `vision/lolvision/voice.py` | ✅ F2 热键 + faster-whisper，可跑 |
| 手机桌宠页面（SSE） | `client/pet.html`、`src/mobile/server.mjs` | ✅ 15 项测试通过 |
| DSH 插件宿主接线 | `src/host.mjs`、`src/vision/*` | ✅ `npm test` 122 项，116 通过（6 项因本机无 DSH 老基线） |
| PC 发帧服务 v1 | `phone/pc_capture.py` | ⚠️ 只有 `/frame`、`/info`，待扩展 |

未完成（本次要做的）：

1. `phone/pc_capture.py` 扩展：OCR 字段、`/tick`、`/apk`
2. `phone/fields.yaml`：ROI 与正则（占位默认值，暂不校准）
3. `phone/desktop_runner.py`：桌面验证版（拉流 + YOLO + DeepSeek + 打印）
4. `android/`：Kotlin 工程（TFLite + WebView + 设置页 + 模型热换）
5. `.github/workflows/android.yml`：云端出 APK
6. `phone/README.md`：安装与使用

## 4. 协议（已定稿，直接照做）

`GET http://<PC_IP>:8770/tick?since=<seq>`

```json
{
  "seq": 1287,
  "ts": 1761650000.123,
  "size": [640, 360],
  "image": "<base64 jpeg, 约 30KB>",
  "ocr": {
    "values": {"hp": [87, 100], "ammo": [7, 30], "money": 3200, "timer": 23, "score": [7, 5]},
    "unstable": ["ammo"],
    "calibrated": false,
    "ms": 42.1
  }
}
```

- `since` 与当前 seq 相同 → 返回 `{"unchanged": true, "seq": n}`，手机端跳过推理。
- `/frame` 保留原始 JPEG；`/apk` 提供 `phone/dist/*.apk` 下载，供手机一键安装。

## 5. 手机端 Android 工程要求

- 包名 `com.lolcompanion.pet`，minSdk 26，targetSdk 34，Kotlin，AGP 8.5.2 + Gradle 8.7 + JDK 17。
- 依赖：`androidx.appcompat:1.6.1`、`com.squareup.okhttp3:okhttp:4.12.0`、`org.tensorflow:tensorflow-lite:2.14.0`、`tensorflow-lite-gpu:2.14.0`。不要用 Compose（降低编译风险）。
- 服务：`CompanionService` 前台服务，循环拉 `/tick` → 解码 → TFLite → 聚合 → 调 DeepSeek → 广播给 Activity。
- 界面：`MainActivity` 用 WebView 加载 `assets/pet.html`，通过 `evaluateJavascript("window.petSay([...],holdMs)")` 推气泡；设置页用 `AlertDialog + EditText`（PC 地址、API Key、模型路径、触发间隔）。
- 模型热换：`/Android/data/com.lolcompanion.pet/files/models/current.tflite` + `labels.txt`，应用内可点选或从 URL 下载；缺失时降级为「只调 DeepSeek、无检测」。
- 推理约定：输入 `1×640×640×3` float32（0–1），输出 `1×84×8400`；后处理为置信度过滤 + NMS。
- 明文 HTTP：`android:usesCleartextTraffic="true"`（仅局域网）。

`client/pet.html` 需补两个全局函数供 WebView 调用：`window.petSay(lines, holdMs)`、`window.petStatus(text)`；无 `?t=` 参数时不要报错，显示「等待连接」。

## 6. DeepSeek 调用

- Base：`https://api.deepseek.com/chat/completions`（OpenAI 兼容）
- model：`deepseek-flash`
- Key：手机端 SharedPreferences 或环境变量，**不写进 APK**
- 超时 8s，失败重试 1 次，再失败就推一条本地兜底文案
- 触发：死亡（OCR hp=0 或画面变灰）为主；可配置每 30 秒一次轻量点评
- 每次输入约 1.5–2.5k token，输出 100 token，成本约 ¥0.01/次

送给模型的内容：`{scene:{objects:[{cls,count,region}]}, hud:{...}, events:[...], ask:"..."}`，不带原始帧。

## 7. YOLO 选型

| 模型 | 文件 | 单帧（8 Gen 3 GPU） |
| --- | --- | --- |
| yolov8n @640 | ~6.5MB fp16 | 10–15ms |
| yolov8s @640 | ~22MB fp16 | 20–30ms |

导出：`yolo export model=yolov8n.pt format=tflite imgsz=640 half=True`
第一版先用官方 COCO 权重验证链路，再换自训模型。

## 8. 构建与出包

- 本机已下载：`D:\toolchain\cmdline-tools.zip`（153MB，未解压）；JDK 17 下载失败，需重试（Temurin 17 zip）。
- 首选 GitHub Actions：`.github/workflows/android.yml`，`ubuntu-latest` + `actions/setup-java@v4`（temurin 17）+ `gradle/actions/setup-gradle@v4`，执行 `gradle assembleDebug`，用 `softprops/action-gh-release@v2` 把 APK 挂到 Release（**不要用 Artifact，手机下载需登录**）。
- 备选：本机装 SDK 后 `gradle assembleDebug`，产物拷到 `phone/dist/`，用 `/apk` 下载。

## 9. 环境事实

- Windows 11、12 核、Intel Arc B580、Node 22.22、Python 3.13（venv 在 `vision/.venv`）
- DSH 0.2.1-rc.2 已装在 `D:\dsh`（`npm-global` + `home`），插件已 link 进 web/headless profile；启动：`D:\dsh\start-dsh.cmd`
- 英雄联盟：`E:\WeGameApps\英雄联盟（含经典模式）`
- 手机：iQOO Neo10，骁龙 8 Gen 3，12GB
- `D:\WORKBUDDY` 是 WorkBuddy 的安装目录，**不是工作空间**；工作空间是 `D:\WBSJ\AIPW`

## 10. 踩过的坑（别重复）

1. RapidOCR 1.2.3 的 `use_text_det` 只能构造时传，按调用传会被忽略；单行识别要直调 `engine.text_recognizer([img])`，否则低置信单字符（如「8」）会被过滤。
2. npm 全局装到自定义 prefix 时，pnpm 必须同 prefix 且 `PATH` 用 `/d/...` 形式，否则 `dsh plugin` 找不到 pnpm。
3. `dsh plugin add` 触发 HMR，插件可能被卸载，需重启 `dsh web` 并用新 token 打开。
4. PowerShell 脚本被执行策略拦截：`Set-ExecutionPolicy -Scope Process Bypass`。
5. 不能从 bash 调 `powershell.exe` 或 `csc.exe`（安全策略拒绝）；COM 创建快捷方式也被拦。
6. `cv2.imencode` 质量参数用 `[int(cv2.IMWRITE_JPEG_QUALITY), 70]`。
7. 手机页面必须前台常亮才能持续接收（浏览器后台会被冻结）。

## 11. 验收标准

1. `curl http://127.0.0.1:8770/tick` 返回 JSON，含非空 `image` 与 `ocr.values`。
2. `curl -o test.apk http://127.0.0.1:8770/apk` 能下到 APK。
3. APK 安装后：填 PC 地址与 Key → 开始 → 手机出现桌宠气泡，内容来自 DeepSeek。
4. 关掉 PC 服务，手机端显示「连接断开」而不是崩溃。

## 12. 第一次回复请先做

1. 读 `phone/pc_capture.py` 与 `client/pet.html`，确认现状；
2. 扩展 `/tick` + `phone/fields.yaml`，用 `curl` 验证；
3. 再动 Android 工程。
