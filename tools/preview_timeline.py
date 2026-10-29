"""复刻并验证 Timeline.digest()：按时刻聚合成行 + 行级同类合并。"""

MAX_POINTS = 12
FIELD_WEIGHT = {"h": 50, "e": 40, "s": 25, "a": 10}


class S:
    def __init__(self, at, hp, sh, am, ar, e, region="c", timer=None):
        self.at, self.hp, self.sh, self.am, self.ar = at, hp, sh, am, ar
        self.e, self.region, self.timer = e, region, timer


def base_text(s):
    parts = [f"h{s.hp}" if s.hp is not None else "h?"]
    if s.sh is not None:
        parts.append(f"s{s.sh}")
    if s.am is not None or s.ar is not None:
        parts.append(f"a{s.am if s.am is not None else '-'}/{s.ar if s.ar is not None else '-'}")
    parts.append(f"e{s.e}")
    if s.timer is not None:
        parts.append("t%d:%02d" % (s.timer // 60, s.timer % 60))
    return "0.0 " + " ".join(parts)


def diff(before, after):
    out = []
    if after.hp != before.hp:
        out.append(("h", "h?" if after.hp is None else f"h{after.hp}"))
    if after.sh != before.sh:
        out.append(("s", "s?" if after.sh is None else f"s{after.sh}"))
    if after.am != before.am or after.ar != before.ar:
        out.append(("a", f"a{after.am if after.am is not None else '-'}/"
                         f"{after.ar if after.ar is not None else '-'}"))
    if after.e != before.e:
        tag = after.region[0] if after.e > 0 else ""
        out.append(("e", f"e{after.e}{tag}"))
    return out


def bonus(s):
    v = 0
    if s.hp == 0:
        v += 60
    if s.hp is None:
        v += 40
    if s.e > 0:
        v += 20
    if s.am == 0:
        v += 15
    return v


def digest(samples):
    if len(samples) < 2:
        return "(空)"
    base = samples[0]

    rows = []           # 每行: {fields, text, weight, t0, t1}
    ref = base
    for i, s in enumerate(samples[1:], 1):
        changes = diff(ref, s)
        if not changes:
            continue
        fields = tuple(f for f, _ in changes)
        rows.append({
            "fields": fields,
            "text": " ".join(t for _, t in changes),
            "weight": sum(FIELD_WEIGHT.get(f, 5) for f in fields) + bonus(s) + i,
            "t0": (s.at - base.at) / 1000.0,
            "t1": (s.at - base.at) / 1000.0,
        })
        ref = s

    merged = []
    for row in rows:
        prev = merged[-1] if merged else None
        if (prev and len(row["fields"]) == 1 == len(prev["fields"])
                and prev["fields"] == row["fields"]):
            prev["t1"] = row["t1"]
            prev["text"] = row["text"]
            prev["weight"] = max(prev["weight"], row["weight"])
            continue
        merged.append(dict(row))

    lines = [base_text(base)]
    weights = [1_000_000]
    for m in merged:
        span = "" if abs(m["t1"] - m["t0"]) < 0.05 else "~%.1f" % m["t1"]
        lines.append("+%.1f%s %s" % (m["t0"], span, m["text"]))
        weights.append(m["weight"])

    if len(lines) <= MAX_POINTS:
        return "\n".join(lines)
    keep = [0] + sorted(range(1, len(lines)), key=lambda i: -weights[i])[:MAX_POINTS - 1]
    return "\n".join(lines[i] for i in sorted(keep))


def show(title, samples):
    out = digest(samples)
    print("=" * 66)
    print(title)
    print("=" * 66)
    print(out)
    print(f"[{len(samples)} 点 -> {len(out.splitlines())} 行, {len(out)} 字符]\n")


show("场景 1：被左边一个人打死", [
    S(0, 100, 50, 30, 60, 0, timer=94),
    S(800, 100, 50, 30, 60, 0),
    S(1300, 69, 19, 28, 60, 1, "left"),
    S(1600, 69, 0, 28, 60, 1, "left"),
    S(2100, 69, 0, 0, 60, 1, "left"),
    S(2400, 0, 0, 0, 60, 1, "left"),
])

show("场景 2：掉血后回血（h42 不能被吞）", [
    S(0, 100, 50, 30, 60, 0),
    S(500, 71, 50, 30, 60, 1, "left"),
    S(1000, 42, 50, 30, 60, 1, "left"),
    S(1500, 65, 50, 30, 60, 0),
    S(2000, 88, 50, 30, 60, 0),
])

long = []
hp = 100
for i in range(80):
    hp = max(0, hp - (7 if i % 3 == 0 else 0))
    long.append(S(i * 500, hp, 50, 30 - (i % 10), 60, 1 if 20 < i < 60 else 0,
                  "left", timer=90 - i // 2))
show("场景 3：交火 40 秒（弹药每 0.5 秒变、血量每 1.5 秒掉 7）", long)

show("场景 4：架枪 20 秒没变化", [S(i * 500, 100, 50, 30, 60, 0, timer=60) for i in range(40)])

show("场景 5：被秒杀 + 血条消失（切观战视角）", [
    S(0, 100, 50, 30, 60, 0, timer=94),
    S(320, 100, 50, 30, 60, 0),
    S(640, 0, 0, 30, 60, 1, "center"),
    S(960, None, None, None, None, 0),
])
