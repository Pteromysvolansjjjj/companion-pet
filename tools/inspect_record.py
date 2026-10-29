"""盘点录制产物：每段时长、时间轴覆盖、断档。"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAP = ROOT / "logs" / "capture"
FFPROBE = "D:/WBSJ/tools/ffmpeg-master-latest-win64-gpl/bin/ffprobe.exe"

PATTERN = re.compile(r"hud-(\d{8})-(\d{6})\.mp4$")


def probe(path: Path) -> tuple[float, int]:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,duration",
             "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=40).stdout
        data = json.loads(out)
        stream = data["streams"][0]
        duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
        frames = int(stream.get("nb_frames") or 0)
        return duration, frames
    except Exception:
        return 0.0, 0


def main() -> int:
    files = []
    for path in CAP.glob("hud-*.mp4"):
        match = PATTERN.search(path.name)
        if not match:
            continue
        started = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
        files.append((started, path))
    if not files:
        print("没有录制文件")
        return 1
    files.sort()

    print(f"{'开始':<10}{'时长':>8}{'帧数':>8}{'体积MB':>9}{'MB/分':>8}  文件")
    print("-" * 76)
    total = 0.0
    rows = []
    for started, path in files:
        size = path.stat().st_size / 1024 / 1024
        duration, frames = probe(path)
        total += duration
        rate = size / duration * 60 if duration > 0 else 0
        note = "" if duration > 1 else "  (未收尾/过短)"
        print(f"{started.strftime('%H:%M:%S'):<10}{duration:>7.1f}s{frames:>8}"
              f"{size:>9.1f}{rate:>8.0f}{note}")
        rows.append((started, duration, size))

    span = (files[-1][0] - files[0][0]).total_seconds()
    used = 0.0
    gaps = []
    for index, (started, duration, _size) in enumerate(rows):
        used += duration
        if index + 1 < len(rows):
            next_start, _, _ = rows[index + 1]
            gap = (next_start - started).total_seconds() - duration
            if gap > 2:
                gaps.append((started, gap))

    print("-" * 76)
    print(f"段数        {len(files)}")
    print(f"时间跨度    {span / 60:.1f} 分钟（{rows[0][0].strftime('%H:%M:%S')} → {rows[-1][0].strftime('%H:%M:%S')}）")
    print(f"有效录制    {used / 60:.1f} 分钟   覆盖率 {used / span * 100:.0f}%")
    print(f"断档合计    {span - used:.0f} 秒，共 {len(gaps)} 处")

    if gaps:
        print("\n最大的几处断档：")
        for started, gap in sorted(gaps, key=lambda item: -item[1])[:6]:
            print(f"  {started.strftime('%H:%M:%S')} 之后断了 {gap:.0f} 秒")

    game = [row for row in rows if row[2] / max(row[1], 1) * 60 > 20]
    print(f"\n高码率段（>20MB/分钟，基本就是对局画面）：{len(game)} 段，"
          f"合计 {sum(row[1] for row in game) / 60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
