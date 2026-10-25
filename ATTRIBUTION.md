# 来源与许可说明

## 本项目代码

本项目自主编写的代码、文档和脚本采用 [MIT 许可证](LICENSE)。允许使用、修改、分发和商用，须保留版权及许可声明。包元数据为 `license: "MIT"`；`private: true` 只防止误发布到 npm，不影响 GitHub 开源。

数据连接、DSH 适配及 WPF 窗口代码独立实现；移动、缩放、设置等交互参考上游并按本项目需求重新实现。第三方美术、证书及保留的上游声明不因根目录 LICENSE 自动取得本项目的 MIT 授权，其来源和权利范围如下。

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
