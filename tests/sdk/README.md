# 使用已安装 DSH 的真实 SDK 验证

在项目目录执行（安装位置作为参数传入）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\sdk\run.ps1 -DshAppDirectory 'E:\deepseek\dsh'
```

脚本用安装包内的 Electron 以 Node 模式运行，从 `resources/app.asar` 导入真实 Cordis、工具、Agent 和 Session SDK，并挂载本项目 `src/host.mjs`。宿主的日常桌面进程不会启动、重启或被修改；测试使用独立的内存会话，不访问用户聊天记录、profile 或凭据。

游戏连接、桌面窗口和模型响应均由进程内测试替身提供。不会请求游戏端口、联网、调用付费模型、生成真实桌宠窗口或写运行状态、会话设置文件。设置通过 `persistPreferences: false` 保存在测试进程内；文件持久化由独立宿主测试验证。

0.7.0 覆盖真实工具注册与 `execute` 会话上下文，以及以下行为：

- `lol_read`、幂等 start、stop、重新绑定和跨会话隔离。
- 通过真实注册的 `lol_tune` 调整数据档、采样、自动投递间隔、每页停留时间和回复长度偏好；空参数只查询，未绑定会话的设置不影响当前监听。
- 默认主要档、显式主要档以及另一个会话的详细档分别传给 watcher 和动态提示；工具返回的偏好与发给桌宠设置的 `preferences.values` 一致。
- 仅设置 500 毫秒投递会自动将较慢采样调到 500 毫秒；499 毫秒不提前投递，500 毫秒时可投递。
- 动态提示明确每轮按最新状态和期间事件综合判断，不逐个事件排队补播；该断言检查提示协议，不代表真实模型一定遵守。
- 修改后的偏好进入下一次模型调用；`replyLength: 0` 撤掉总字数偏好。
- 超过两页的自动、手动纯文本回复本地分页，保留组合 emoji 和重音字符；不会为了长度多调用一次模型，不向历史注入格式修复消息。
- `<silent/>` 清除气泡；纯自动轮在主 Chat 隐藏，手动插话形成的混合轮仍可见，原始回复记录保留。
- 桌宠 `onChatSubmit` 输入携带当前绑定和请求编号，经真实 `createUserMessage`、`agent.followup` 进入原会话，`source.kind` 为普通人工输入的 `user`；换行、空格、emoji 原样保留。`chat:result` 在入队成功后确认，重复请求重发确认但不重复写历史或调用模型。
- 空闲时桌宠输入唤醒原智能体；原模型应答尚未结束时输入先确认入队，既不中断原应答，也不开并行模型请求，随后在原会话正常下一轮处理。
- 通过真实 Session 投影和本插件实际 Chat 分类器验证桌宠输入及回复均可见；另以 SDK 维护阶段暂缓领取，显式注入 `next-step` 自动上下文，验证它与桌宠 `next-turn` 输入共同领取时混合轮仍可见。这是测试构造的混合场景，正常两个 `followup` 会分别进入下一轮，不会被插件强制合并。测试不会创建桌宠专属会话或模型线路。
- 显示协议包含 `{type: 'reply', lines, holdMs}`，卸载清理幂等。

桌面替身提供 `onChatSubmit` 和 `chat:result` 确认链路，不提供 `onPlayback` 和 `onPreferences`，使用无背压测试接口；它不是产品里的“关闭等待”开关。真实整条播放完成前暂停自动投递、设置界面的回传和保存、鼠标三击/右键、中文输入法与窗口焦点由宿主/桌面协议及实际窗口验收覆盖，不能从本 SDK 测试推断。`ok: true` 代表这条 SDK 集成链路通过；Chat 断言验证消息记录和实际显示分类逻辑，没有启动完整 DSH 图形界面。

适用前提：安装目录包含 `DeepSeek Harness.exe` 与 `resources/app.asar`，SDK 仍采用已核验的包布局。具体检查类别与本地模拟调用次数由成功结果的 `checks` 和 `fakeModelCalls` 报告。测试设置 20 秒截止时间；失败会返回非零退出码。
