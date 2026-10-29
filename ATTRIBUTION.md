# 来源与许可说明

## 上游项目（本仓库的底层来源）

- 上游仓库：[jinghuashuiyue2004/dsh-plugin-lol-companion](https://github.com/jinghuashuiyue2004/dsh-plugin-lol-companion)
- 上游作者：**`jinghuashuiyue2004` —— 本仓库底层实现的原作者**
- 本 fork 起点：上游 v0.7.1；本仓库版本号 `0.7.1-screen.1`
- 上游许可：MIT
- **继承自上游**：数据连接（LCU + Live Client Data）、DSH 宿主适配、WPF 桌宠窗口与桌面通信、
  会话与调度协议、`lol_tune` 工具、原有测试与文档骨架。
- **本 fork 新增**：屏幕 OCR 数据源、手机端 YOLO 视觉链路（`phone/` + `android/`）、语音输入，
  以及配套文档（`docs/SCREEN_OCR.md`、`docs/VOICE.md`、`phone/README.md`）。
- 上游的演示视频与截图版权属于原作者，展示的是**上游原版**行为，不代表本 fork 的实际效果。

本仓库不是 GitHub 意义上的 fork 标记仓库（历史重新推送过），因此在这里显式声明来源与作者。

## 本项目代码

本项目自主编写的代码、文档和脚本采用 [MIT 许可证](LICENSE)。允许使用、修改、分发和商用，须保留版权及许可声明。包元数据为 `license: "MIT"`；`private: true` 只防止误发布到 npm，不影响 GitHub 开源。

数据连接、DSH 适配及 WPF 窗口代码来自上游项目（见上一节），本 fork 未重写这部分；移动、缩放、设置等交互同样来自上游。本 fork 新增的 `phone/`、`android/`、屏幕 OCR 与语音相关代码沿用同一 MIT 许可。第三方美术、证书及保留的上游声明不因根目录 LICENSE 自动取得本项目的 MIT 授权，其来源和权利范围如下。

## 人物素材

- 上游项目：[DeepSeek-Balance-Whale-Widget](https://github.com/MeteorNOX/DeepSeek-Balance-Whale-Widget)。
- 原始图片：[DSniang02.png](https://github.com/MeteorNOX/DeepSeek-Balance-Whale-Widget/blob/main/assets/DSniang02.png)，本项目保留完整原图。
- 派生文件：`assets/DSniang02-character.png`，通过手工轮廓遮罩处理透明度，去除原图气泡等区域；没有另行生成新人物。原图、派生图哈希和处理方法见[处理记录](assets/DSniang02-character-provenance.json)。
- 本发行版根据项目维护者的授权确认保留原素材。图片不适用本项目代码的 MIT 许可证，使用、修改和再次分发仍以其权利人的授权为准；本项目不对第三方图片授予额外权利。
- 上游明确将美术素材排除在其代码 MIT 许可之外。保留的[上游素材声明](assets/SOURCE-PROVENANCE.md)是来源记录，不是本项目授予的素材许可。

上游声明中的“本仓库”、相对 LICENSE 链接及联系渠道均指上游项目语境。本项目没有将图片声明为原创或 MIT 素材。素材来源或授权如有问题，可在本仓库提交 Issue，说明具体文件和依据。

## Riot 本地接口证书

- 文件：`assets/riotgames.pem`。
- 来源：[Riot 官方证书文件](https://static.developer.riotgames.com/docs/lol/riotgames.pem)。
- SHA-256 指纹：`CA:8C:9D:32:5B:4C:DC:46:4C:6C:94:A5:85:C8:5E:91:EC:23:D4:0B:A5:BF:3A:E2:82:2B:95:1A:4A:50:4E:A3`。
- 这是用于验证本机 Riot HTTPS 接口的公开 CA 证书，不是用户私钥；项目未关闭 TLS 验证。

本项目不是 Riot、DeepSeek 或素材作者提供的官方 LOL 插件。
