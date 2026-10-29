# 语音输入

按住热键说话，松开后本地转写成文字，作为**普通的用户消息**进入当前对话——不需要打开任何窗口，也不需要切出游戏。

## 1. 用法

1. 先在原对话里启动陪玩（`lol_watch_start`）。语音只在绑定期间生效，避免把话发进无人的会话。
2. 游戏中按住 **F2** 说一句话，松开。托盘提示与气泡会先回显识别结果，随后是模型对这一句的回复。
3. 若与游戏按键冲突：`lol_tune` 里改 `voiceHotkey`，例如 `Ctrl+Alt+F2`、`F8`、`~`。
4. 想要「按一下开始、再按一下结束」：`voiceMode: "toggle"`（会自动在静音 0.8 秒后结束）。

## 2. 走的是哪条通路

```
RegisterHotKey → sounddevice 采集 → 能量 VAD 断句 → faster-whisper（本地）
  └─ JSONL: {"type":"voice","text":"..."}
      └─ src/host.mjs: submitManual() 与桌宠输入条完全相同的路径
          └─ createUserMessage({source:{kind:'user'}}) → agent.followup()
```

关键点：语音转写用的是 `source.kind = "user"`，**不是** `lol-companion`。因此：

- 它会出现在原来的 Chat 里，和手动打字一样；
- 它按"人类插话"处理，会顶掉过期的自动轮次（`suppressedTurns` 逻辑），不会被当成游戏数据；
- 模型对它的回应正常显示在气泡里，与自动播报共用分页与停留时间。

## 3. 本地模型与延迟

默认 `faster-whisper-small`（int8，CPU）：

| 环节 | 实测 |
| --- | --- |
| 模型加载 | 2.8 s（启动后台预热，之后常驻） |
| 6 秒中文语音转写 | 1.7–1.9 s |
| 端到端（松手到文字进入会话） | 约 2.0–2.6 s |

启动后模型会在后台预热；如果恰好在预热完成前说话，第一句要多等约 2–3 秒（实测 4.7 s）。

识别质量实测（本地 TTS 合成的三句）：

```
打野小心一点，对面中单不见了。      → 打野小心一点 对面中单不见了
我这波要回城买装备，你先守一下塔。  → 我这波要回城买装备 你先守一下塔
小龙还有三十秒刷新，我们集合。      → 小龙还有30秒刷新 我们集合
```

标点会被省略（对模型不影响）。想要更快可以把 `voiceModel` 设为 `base`，更准则设 `medium`（约 1.5 GB，延迟翻倍）。

## 4. 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `voiceMode` | `push` | `push` 按住说话 / `toggle` 按一下开始 / `off` 关闭 |
| `voiceHotkey` | `F2` | 支持 `Ctrl`、`Alt`、`Shift`、`Win` 组合 |
| `voiceEcho` | `bubble` | 识别结果先在气泡回显；`tray` 只写托盘；`off` 不回显 |
| `voiceModel` | `small` | `tiny` / `base` / `small` / `medium` |
| `voiceLanguage` | `zh` | 传给 Whisper 的语言 |
| `voiceDevice` | 系统默认 | 麦克风设备索引（`vision/probe.py` 或启动日志里能看到列表） |

侧车启动时会列出可用输入设备，默认是系统默认麦克风。当前机器识别到 `Wireless Mic Rx`（默认）与 `SoncieMeory SA150GM`。

## 5. 设计与边界

- **不是键盘钩子**：用 `RegisterHotKey` 注册组合键，系统只在该组合触发时通知本进程；不监听、不记录其他按键。按下后靠 `GetAsyncKeyState` 轮询判断松开——仍然只读这一个键的状态。
- **不写游戏聊天**：不模拟按键、不注入，也不会把文字发到游戏内聊天框。文字只进 DSH 会话。
- **独占全屏**：热键仍然有效（系统级），但桌宠气泡看不到；建议无边框/窗口模式。
- **热键被占用**：会返回 `hotkey_register_failed`，换一个组合键即可。
- **隐私**：音频只在内存中处理，不落盘、不上传；转写文本随会话上下文交给会话所选模型（与打字相同）。

## 6. 排错

| 现象 | 处理 |
| --- | --- |
| 按 F2 没反应 | 热键被其他软件占用；换 `voiceHotkey`。或 `voiceMode` 被设为 `off` |
| 提示「没有听清」 | 说话太短（<0.3 s）或全程低于噪声阈值；靠近麦克风重说 |
| 识别成无关内容 | 环境噪声大或模型过小；换 `small`/`medium`，或在 `voice.py` 的 `initial_prompt` 里加入你的常用术语 |
| 转写很慢 | 关闭其他占用 CPU 的程序；或把 `voiceModel` 降到 `base` |
| 侧车起不来 | 运行 `vision/setup.ps1` 重装依赖；`npm run vision:voice` 可单独验证模型与识别 |
