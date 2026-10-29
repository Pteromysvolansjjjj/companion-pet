"""用实际生成的 trail 文本，算加入「让模型自己理解过程」后的真实成本。"""
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("pt", "logs/preview_timeline.py")
pt = importlib.util.module_from_spec(spec)
sys.stdout = open("logs/_quiet.txt", "w", encoding="utf-8")
spec.loader.exec_module(pt)
sys.stdout = sys.__stdout__

S = pt.S
digest = pt.digest

battle = [
    S(0, 100, 50, 30, 60, 0, timer=94),
    S(800, 100, 50, 30, 60, 0),
    S(1300, 69, 19, 28, 60, 1, "left"),
    S(1600, 69, 0, 28, 60, 1, "left"),
    S(2100, 69, 0, 0, 60, 1, "left"),
    S(2400, 0, 0, 0, 60, 1, "left"),
]
long_ = []
hp = 100
for i in range(80):
    hp = max(0, hp - (7 if i % 3 == 0 else 0))
    long_.append(S(i * 500, hp, 50, 30 - (i % 10), 60, 1 if 20 < i < 60 else 0,
                   "left", timer=90 - i // 2))
idle = [S(i * 500, 100, 50, 30, 60, 0, timer=60) for i in range(40)]

OLD_SYSTEM = 433
NEW_SYSTEM_EXTRA = 104      # 新增的 trail 说明

payload_old = {
    "game": "valorant",
    "notes": ["你只剩 23 血，快没了", "左边有 2 个对面，刚露头"],
    "events": [{"kind": "enemySpotted", "count": 2, "was": 0, "region": "left"}],
    "recent": ["别急着拉枪线", "这波我帮你盯着"],
    "ask": "此刻你想对他说什么？",
}
old_text = json.dumps(payload_old, ensure_ascii=False, separators=(",", ":"))


def tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return round(cjk / 1.3 + (len(text) - cjk) / 3.5)


REPLY = 30
base_total = tokens(old_text) + OLD_SYSTEM + REPLY
new_system = OLD_SYSTEM + tokens("x" * 0) + round(75 / 1.3 + 29 / 3.5)

print(f"现状：payload {tokens(old_text)} + system {OLD_SYSTEM} + 回复 {REPLY} = {base_total} token\n")
print(f"{'场景':<22}{'trail 字符':>10}{'trail token':>12}{'单次总计':>10}{'倍数':>7}")
print("-" * 62)
for name, trail in (("架枪静止（无变化）", digest(idle)),
                    ("被一个人打死（典型）", digest(battle)),
                    ("交火 40 秒（最密）", digest(long_))):
    t = tokens(trail)
    total = t + new_system + tokens(old_text) + REPLY
    print(f"{name:<22}{len(trail):>10}{t:>12}{total:>10}{total/base_total:>6.2f}x")

print()
print("=" * 62)
print("每小时成本（200 次调用；输入 ¥2/百万，输出 ¥8/百万）")
print("=" * 62)
for name, trail in (("架枪/静止", digest(idle)),
                    ("典型交火", digest(battle)),
                    ("密集交火", digest(long_))):
    total = tokens(trail) + new_system + tokens(old_text) + REPLY
    cost = (total - REPLY) * 200 / 1e6 * 2 + REPLY * 200 / 1e6 * 8
    print(f"  {name:<10} 单次 {total:>4} token -> ¥{cost:.2f}/小时，"
          f"¥{cost * 4 * 30:.0f}/月（每天 4 小时）")

print()
print("对比：全量快照方案（31 帧）单次 897 token -> ¥0.42/小时，多花 45%")
