# 屏幕 OCR 数据源

本 fork 增加了一种数据源：**在本机读屏、识别文字，再把文字按原插件的数据协议投给当前对话**，不调用 LCU（客户端 HTTPS 接口）也不调用 Live Client Data API（观战端口 2999）。

它与接口档共用后续全部链路：采样 → 变化检测 → 批处理 → 模型 → 桌宠气泡，因此分页、停顿、忙碌抑制、`<silent/>`、历史压缩等行为完全一致。

## 1. 数据流

```
游戏窗口（无边框/窗口模式）
  └─ mss 截取客户端区域（BitBlt，不注入、不读内存、不发输入）
      └─ Python 侧车 vision/main.py
          ├─ 固定槽位识别（RapidOCR 识别器，2–30 ms/槽）
          ├─ 中心公告识别（含检测，约 0.7 s/次，5 s 一次）
          └─ JSONL: {"type":"frame", fields, lines, events, ...}
              └─ Node src/vision/connector.mjs  → 与接口档同构的 snapshot
                  └─ src/game/watcher.mjs（原有）→ batcher（原有）→ host（原有）
                      └─ agent.followup('[LOL 实时数据]' + 契约 + JSON)
```

侧车只输出文本与数字，不接触模型、不接触游戏凭据。

## 2. 字段映射

| 屏幕来源 | `sections.screen.data` | 兼容视图 `sections.live.data` |
| --- | --- | --- |
| 顶栏中间计时 | `fields.gameTime`（秒） | `gameData.gameTime` |
| 顶栏两侧比分 | `fields.allyKills` / `fields.enemyKills` | `activePlayer.teamKills` / `enemyTeamKills` |
| 血条/蓝条颜色占比（需校准） | `bars.hp` / `bars.mana` | `activePlayer.healthRatio` / `manaRatio` |
| 等级数字 | `fields.level` | `activePlayer.level` |
| 击杀播报 | `events[]`（`ChampionKill` / `EpicMonsterKill` / `BuildingKill`） | `events.Events`（本批新增） |
| Tab 记分板（默认关闭） | `board.rows[]`（KDA、补刀、原文） | `allPlayers[]`（`championName`/`kills`/`deaths`/`assists`/`minionsKilled`） |
| 全部识别原文 | `lines[]`（`text`/`region`/`score`） | — |

`sections.live.data.estimated` 恒为 `true`，`sections.screen.data.source` 恒为 `"screen_ocr"`：模型能看出这是读数而不是接口事实。

## 3. 击杀事件如何不重复

击杀播报会在屏幕上停留数秒。侧车为每条播报生成一次稳定 `EventID`（按「击杀者>被击杀者」指纹），并在行消失 2 帧后才释放；同一行在后续帧里沿用同一个 ID，原有的 `delta.mjs` / `batcher.mjs` 去重逻辑照常工作。

启动监听后的**第一帧只作为基线**：屏幕上残留的旧播报不会被当成新事件补播——与接口档首次快照的行为一致。

## 4. 能读到什么、读不到什么

能稳定读到：对局时间、双方比分、击杀播报、等级、中心公告（大龙/小龙/团灭等）。
需要校准后可读：血条/蓝条比例。
默认关闭：Tab 记分板（全表 KDA/补刀/装备）。

**读不到**：金币、技能冷却精确秒数、装备被动、视野得分、队友具体位置、地图迷雾信息——这些在默认 HUD 上根本没有文字。任何声称能读到的实现，要么改了 HUD 设置，要么在猜。

因此屏幕档的能力边界是「时间、比分、击杀与公告、等级」，比接口档窄。需要完整字段时用 `lol_tune` 切回 `dataSource: "api"`。

## 5. 性能

单帧成本（合成 1080p、12 核 CPU 实测）：

| 区域 | 方式 | 耗时 | 频率 |
| --- | --- | --- | --- |
| 顶栏三格 | 识别器 | 约 17 ms | 0.7 s |
| 击杀播报 6 行 | 识别器 | 约 200 ms | 1.2 s |
| 中心公告 | 检测+识别 | 约 760 ms | 5 s |
| 等级 | 识别器 | 约 5 ms | 1.5 s |

稳态约 **0.35–0.5 核**。另外有预算保护：上一帧超过采样间隔的 60% 时，本帧跳过含检测的区域，避免和游戏抢 CPU。

## 6. 校准（重要）

默认布局按 16:9、100% UI 缩放估算，写在 `vision/lolvision/layout.py`。第一次使用请这样核对：

```powershell
npm run vision:probe                      # 直接读当前游戏窗口
python vision/probe.py --image shot.png --save annotated.png   # 用截图，并画出所有区域框
```

`annotated.png` 上会画出每个区域（绿框）、每个槽位（黄线）、顶栏分区（橙框）与字段框（洋红框）。对照实际画面，把偏差写进 `vision/layout.json`（只需写要改的键，其余沿用默认）：

```json
{
  "regions": {
    "feed": { "rect": [0.62, 0.055, 0.37, 0.26], "rows": 6 },
    "center": { "rect": [0.255, 0.155, 0.415, 0.15] }
  },
  "fields": {
    "level": { "rect": [0.372, 0.905, 0.028, 0.04] }
  }
}
```

规则：`rect` 是相对游戏客户端区域的 `[x, y, w, h]`（0–1）；`rows`/`gap` 用于把一条带拆成多行；`parts` 用于把顶栏拆成比分/计时/比分三格；`everyMs` 控制频率。JSON 文件会在启动时与默认值合并，不必抄全量。

UI 缩放不是 100% 时，HUD 元素未必等比缩放，务必重新校准一次。

## 7. 配置

`config.json`（与插件同目录）或 `lol_tune`：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `dataSource` | `screen` | `screen` 或 `api`，切换立即生效 |
| `visionIntervalMs` | `1000` | 截屏+识别节流 |
| `screenStaleMs` | `4000` | 超过此时间没有新帧即视为不可用（例如切出游戏） |
| `visionPython` | `vision/.venv/Scripts/python.exe` | 自定义解释器路径 |
| `vision` | `true` | 关掉后完全不启动侧车 |

## 8. 排错

| 现象 | 原因与处理 |
| --- | --- |
| 一直「等待客户端数据」 | 游戏不是无边框/窗口模式，或不在前台。独占全屏既盖不住桌宠，也取不到稳定画面 |
| `VISION_RUNTIME_MISSING` | 未装依赖：运行 `vision/setup.ps1` |
| 字段时有时无 | 该区域被特效/技能图标遮挡，或布局有偏差：用 `probe` 出图核对 |
| 数字读错（如 8 读成 ¥8） | 识别器常见现象；解析层会剥离非数字字符并做范围校验，仍会偶发。契约里已要求模型不要据此编造精确值 |
| 事件重复播报 | 确认 `feed` 区域只覆盖播报列；把 `rows` 调大到覆盖全部可见行 |
| CPU 偏高 | 把 `center.everyMs` 调大、或把 `board.enabled` 保持 `false` |
