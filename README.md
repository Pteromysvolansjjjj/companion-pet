# LOL 陪玩数据插件（手机 YOLO 视觉改造版）

**把英雄联盟实时数据接进你原有的 DeepSeek Harness 对话，让你养的角色通过桌宠陪你玩。**

## 来源与致谢（重要）

本项目 fork 自 [**jinghuashuiyue2004/dsh-plugin-lol-companion**](https://github.com/jinghuashuiyue2004/dsh-plugin-lol-companion)（v0.7.1，MIT）。

**上游作者 `jinghuashuiyue2004` 是本仓库底层实现的原作者。** 数据连接、DSH 宿主适配、
WPF 桌宠窗口、会话协议、配置与调度这些基础能力全部来自他的项目；本项目只在其之上改了
**画面数据的获取与推理路径**，没有他的工作就没有这个仓库。

下面的演示视频是**原作者的视频**，展示的是**上游原版**的行为（走官方接口那条路），不是本 fork 的实拍：

> 演示视频（原作者）：【我把大肥鱼拉到LOL当陪玩娘了】 https://www.bilibili.com/video/BV1axHV6EErM/?share_source=copy_web&vd_source=63af640136307ef4b250dddc1f4d121b

## 与原项目的区别：手机 YOLO 视觉 vs 官方接口

| | 上游原版 | 本项目（fork） |
| --- | --- | --- |
| 数据来源 | **直接调用 LoL 官方本地接口**（LCU + Live Client Data），拿结构化对局数据 | PC 只采集画面 + OCR 读少量 HUD 数字，**画面理解交给手机端 YOLO（TFLite）** |
| 视觉推理位置 | 无（不读像素） | **手机端**（骁龙 8 Gen 3，YOLOv8 tflite），不占 PC 算力 |
| 送给模型的内容 | 接口事件与状态 | `{game, scene, hud, events}`：YOLO 检出的类别/数量/画面三分区 + OCR 数字 + 事件 |
| 适用场景 | 依赖客户端与对局，联机可用 | 面向**单机/离线与人机局**；靠像素理解画面，不依赖接口 |

本项目**保留了接口路径**（`lol_tune` 里 `dataSource: "api"` 可切回），但主推的是手机 YOLO 这条链路：
手机同一 APK 按 `--profile` 切换不同游戏的模型（瓦罗兰特 / 三角洲 / LoL），详见 [phone/README.md](phone/README.md)。

插件读取数据源后交给当前绑定的对话，再把模型的最终纯文本回复显示在桌宠气泡里。性格、知识、记忆和说什么，由原会话、用户及其已有工具决定。

当前版本：**0.7.1-screen.1**（本 fork）。运行环境：**Windows + DeepSeek Harness**；已验证宿主版本为 **0.2.1-rc.2**。

## 本 fork 的改动

在原有接口数据源之外，增加了三条本机链路，其余行为不变：

- **屏幕 OCR 数据源（默认）**：不调用 LCU/Live Client API，改为截取游戏窗口，本地识别顶栏计时/比分、击杀播报、等级与中心公告，按原有 `lol_update` 协议投给同一会话。见 [docs/SCREEN_OCR.md](docs/SCREEN_OCR.md)。
- **手机端视觉推理**：PC 只负责抓帧 + OCR 数字（`phone/pc_capture.py` 的 `/tick`），画面理解交给手机上的 YOLO（`android/`，TFLite），融合成 `{game, scene, hud, events}` 再调 DeepSeek。按 `--profile` 切换游戏档位与模型。见 [phone/README.md](phone/README.md)。
- **语音输入**：按住热键（默认 F2）说话，本地 Whisper 转写成文字，作为普通用户消息进入原对话，不打开窗口、不注入游戏。见 [docs/VOICE.md](docs/VOICE.md)。

想在数据源之间切换：`lol_tune` 里设 `dataSource: "screen" | "api"`。语音只依赖侧车，与数据源无关。

首次使用本 fork 需安装 Python 侧车（约 500 MB 模型）：

```powershell
powershell -ExecutionPolicy Bypass -File vision\setup.ps1
npm run vision:probe        # 核对识别区域，画出区域框
npm run vision:selftest     # 合成画面 + 事件序列自测
npm run vision:demo         # 打印实际投递给模型的文本
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [用户使用教程](docs/USER_GUIDE.md) | 安装、开始陪玩、游戏内打字、调整设置、常见问题 |
| [屏幕 OCR](docs/SCREEN_OCR.md) | 数据流、字段映射、事件去重、校准、性能与排错 |
| [语音输入](docs/VOICE.md) | 热键、本地模型、延迟实测、配置与排错 |
| [技术文档](docs/TECHNICAL.md) | 架构、接口、数据协议、调度、桌面通信、配置、开发与验证 |
| [验证记录](VERIFICATION.md) | 已执行的检查及其覆盖边界 |
| [贡献指南](CONTRIBUTING.md) | 反馈问题、提交改进、开发验证与设计约定 |
| [来源与许可说明](ATTRIBUTION.md) | 第三方素材与证书来源 |

## 能做什么

- 读取账号等级、大厅与房间、匹配、选人、局内事件及赛后结果，以接口实际提供的字段为准。
- 屏幕档下读取对局时间、双方比分、击杀播报、等级与中心公告；不调用游戏接口。
- 选择主要数据或详细数据，自行调整投递间隔、回复长度偏好和气泡停留时间。
- 等当前整条回复显示完，再把最新状态和期间新增事件交给模型判断下一句。
- 三击桌宠打开输入条，直接向原对话打字；手动输入和回复保留在工作区 Chat。
- 按住热键说话，语音转文字进入同一对话，无需打开输入条。
- 自动游戏轮次在主 Chat 隐藏，原始会话记录和轨迹仍保留。

插件没有独立模型后端，不内置教练人设、攻略知识库或角色记忆。当前仍没有语音播报、游戏内发言或聊天框 OCR。

## 快速开始

1. 将完整项目放到固定目录，例如 `D:\Apps\lol-companion`。确认包含 `package.json` 和 `dist\LolCompanion-0.7.1.exe`；仅下载源码且没有 EXE 时，见[构建方法](docs/TECHNICAL.md#development)。
2. 在 DeepSeek Harness 的 **插件 → 添加插件 → 安装第三方插件** 中填写该目录，安装并启用，然后完整退出并重开 DSH。
3. 回到原来的角色对话，发送：**“我要打英雄联盟，陪我玩。请调用 lol_watch_start。”**
4. 将游戏设置为无边框或窗口模式。三击人物即可在游戏中打字。

模型沿用原会话配置，不需要为此插件另配 API Key。正常使用由 DSH 启动桌宠，不直接双击 EXE。

默认：主要数据、每秒本地采样、每 15 秒最多自动投递一批、回复尽量 20 字。设置可调整；每页固定最多 20 个可见字符，长回复自动分页，总页数不限。

## 数据与费用

本机读取不调用模型；自动投递进入原会话后，按该会话所用模型的实际用量计费。`<silent/>` 只关闭气泡输出，已经发生的推理仍可能计费。详细数据和更短间隔通常会增加用量。

游戏数据通过 LCU 和 Live Client Data API 读取，使用本机 HTTPS GET；不注入游戏、不读进程内存、不模拟游戏输入。接口无法覆盖所有屏幕信息，详细档也不等于完整游戏内部状态。游戏昵称等数据会随上下文交给原会话配置的模型服务。

## 欢迎参与

欢迎大家基于这个项目的框架适配更多游戏。

这个项目的核心思路是：插件负责接收游戏数据、连接用户已有的对话，并通过桌宠展示回复；角色的性格、知识和表达方式由用户培养。欢迎为其他游戏开发数据适配器，让自己的角色陪伴更多游戏。

也欢迎分享自己绘制或有权分享的桌宠图片、气泡样式等素材，让桌宠拥有不同的外观。

- 反馈问题、提出适配建议或分享素材：提交 Issue。
- 贡献代码、文档或素材文件：提交 Pull Request。
- 分享素材时，请注明作者、来源和许可范围，以及是否允许修改、商用和再次分发。

具体参与方式见 [贡献指南](CONTRIBUTING.md)。
欢迎通过 Issues 反馈问题、提出建议，也欢迎 Fork 本项目并提交 Pull Request。修复兼容问题、改进桌宠交互、补充测试和完善文档都很有帮助。开始前请阅读[贡献指南](CONTRIBUTING.md)，保持“插件提供游戏数据，角色由用户培养”的设计原则。

## 许可与素材

本项目自主编写的代码、文档和脚本采用 [MIT 许可证](LICENSE)，允许修改、分发和商用，须保留版权及许可声明。鲸鱼娘图片来自 [DeepSeek-Balance-Whale-Widget](https://github.com/MeteorNOX/DeepSeek-Balance-Whale-Widget)，按维护者确认保留，第三方图片不适用本项目的 MIT 授权；详见[来源与许可说明](ATTRIBUTION.md)。

本项目不是 Riot、DeepSeek 或素材作者提供的官方 LOL 插件。
