# 参与改进

欢迎提交问题、功能建议、代码和文档改进。小修复可以直接提 Pull Request；涉及数据协议、会话调度或产品职责的改动，建议先开 Issue 说明问题和方案，便于讨论兼容性。

## 提交问题

使用仓库的 Issue 模板，提供插件版本、DeepSeek Harness 版本、Windows 版本、复现步骤、预期和实际结果。接口问题请附数据区及错误原因代码；必要时提供清理过的 probe 结果。

不要上传 API Key、LCU 令牌、完整个人聊天、整个 profile 或真实对局日志。界面问题可以附已遮盖账号信息的截图。

## 提交代码

1. Fork 仓库，在自己的分支中修改，一次 PR 尽量解决一个明确问题。
2. 阅读[技术文档](docs/TECHNICAL.md)，保持已有的数据格式、会话隔离和生命周期约定。
3. 运行与改动有关的测试，在 PR 里说明执行了什么、哪些场景尚未验证。
4. 更新受影响的文档；涉及生成文件时同步构建产物。
5. 提交 Pull Request，说明问题、最终行为及验证结果。

## 开发与验证

开发需要 Windows 和 Node.js 22 或以上。纯逻辑测试无需模型 API Key；前端和 SDK 集成测试需要本机安装兼容的 DSH。

```powershell
# 数据与宿主逻辑
node --test tests/game/*.test.mjs tests/host/*.test.mjs

# 修改前端后重新生成 client.js
node scripts/build-client.mjs

# 前端依赖真实宿主归档，路径改为本机安装位置
$env:DSH_ASAR = 'D:\Apps\DeepSeek Harness\resources\app.asar'
node --test tests/client/*.test.mjs

# 修改桌面 C# 后构建并验证
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop\Test-Desktop.ps1
node tests/host/renderer-protocol.mjs

# SDK 集成使用模拟模型，不调用付费模型
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\sdk\run.ps1 -DshAppDirectory 'D:\Apps\DeepSeek Harness'
```

测试替身不等于真实游戏和原生输入法验证。涉及窗口、焦点或输入法时，请另外说明实际环境与手动验收结果；详细边界见[验证记录](VERIFICATION.md)。

## 保持设计边界

- 插件提供游戏事实和桌面交互；性格、知识、事件重点与记忆仍由用户和原会话决定。
- 保持本机只读接口，不新增游戏注入、进程内存读取或游戏输入模拟。
- 区分本地采样和模型调用；保持整条回复播放等待，不改回逐事件生成台词队列。
- 尊重用户设置，明确标记缺失数据和被裁剪的历史，不编造观察事实。
- 手动输入回到原会话，自动轮次只改变展示，不删除真人消息或原始轨迹。

新增能力如果需要改变这些约定，请先讨论。欢迎贡献新的游戏适配器、兼容性修复、测试、文档和有明确分发许可的素材。

## 提交内容与许可

只提交必要的源码、当前构建产物和文档，不提交个人 config.json、data/、node_modules/、.updates/、测试输出或备份。请确认你有权提交相应内容；提交到本项目的自主代码按 [MIT](LICENSE) 提供。第三方素材须单独说明来源和授权，不能用本项目 MIT 声明替代素材许可。
