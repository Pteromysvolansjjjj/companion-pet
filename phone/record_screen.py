"""用 ffmpeg 录屏，给实机测试留材料。

为什么不继续用 Python 抓图存 JPEG：
  mss 抓 2560x1440 单次就要 29-36ms（其中大头是 BGRA->BGR 转换），
  理论上限约 28 FPS，和 OCR 抢 CPU 之后必然掉帧。
  ffmpeg 走 DXGI Desktop Duplication + Intel QSV 硬件编码，
  抓屏和编码都在 GPU 上，CPU 几乎不参与。

用法：
  python logs/record_screen.py --fps 30 --minutes 45
  python logs/record_screen.py --probe          # 只探测可用的抓屏/编码组合
  python logs/record_screen.py --verify 文件    # 验证产物实际帧率
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = [
    Path("D:/WBSJ/tools/ffmpeg/bin/ffmpeg.exe"),
    Path("D:/WBSJ/tools/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe"),
    Path("ffmpeg"),
]


def find_ffmpeg() -> str | None:
    for candidate in CANDIDATES:
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(str(candidate))
        if found:
            return found
    return None


def find_ffprobe(ffmpeg: str) -> str | None:
    sibling = Path(ffmpeg).with_name("ffprobe.exe")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe")


def has_encoder(ffmpeg: str, name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return False
    return name in out


def has_input(ffmpeg: str, name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-devices"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return False
    return name in out


def build(ffmpeg: str, fps: int, out: Path, encoder: str,
          seconds: int | None = None) -> list[str]:
    """ddagrab 抓屏 + 编码。

    实测（2560x1440，12 线程，桌面画面）：
      30fps + libx264 ultrafast   30.00 fps  占 93% 单核
      30fps + h264_qsv            27.40 fps  占 71% 单核
    QSV 反而掉帧：ddagrab 出的是 d3d11 帧，必须 hwdownload 回 CPU 内存，
    再做 bgra->nv12 转换才能喂给 QSV，这段 swscale 是单线程瓶颈。
    libx264 ultrafast 虽然 CPU 高一点，但能稳到名义帧率，所以默认用它。
    """
    if encoder == "qsv":
        video = ["-c:v", "h264_qsv", "-global_quality", "23"]
    else:
        video = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]

    cmd = ([ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-f", "lavfi", "-i", f"ddagrab=framerate={fps}:output_idx=0",
            "-vf", "hwdownload,format=bgra"] + video)
    # -t 是输出选项，必须放在输出文件名之前，否则会被忽略、一直录到磁盘满
    if seconds:
        cmd += ["-t", str(seconds)]
    return cmd + [str(out)]


def build_gdi(ffmpeg: str, fps: int, out: Path, encoder: str,
              seconds: int | None = None) -> list[str]:
    if encoder == "qsv":
        video = ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "23"]
    else:
        video = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"]
    cmd = ([ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-f", "gdigrab", "-framerate", str(fps), "-i", "desktop"] + video)
    if seconds:
        cmd += ["-t", str(seconds)]
    return cmd + ["-pix_fmt", "nv12", str(out)]


def probe(ffmpeg: str, seconds: int = 12, fps: int = 30) -> None:
    """每种组合录一小段，比较实际帧率、体积、耗时。"""
    out_dir = ROOT / "logs" / "capture"
    out_dir.mkdir(parents=True, exist_ok=True)
    encoders = [e for e in ("qsv", "x264") if has_encoder(ffmpeg, f"h264_{e}") or e == "x264"]
    combos = []
    for encoder in encoders:
        combos.append(("ddagrab", encoder,
                       build(ffmpeg, fps, out_dir / "_probe.mp4", encoder, seconds)))
        combos.append(("gdigrab", encoder,
                       build_gdi(ffmpeg, fps, out_dir / "_probe.mp4", encoder, seconds)))

    print(f"{'抓屏':<10}{'编码':<8}{'名义fps':>8}{'实际fps':>9}{'体积MB':>9}{'耗时s':>8}")
    print("-" * 56)
    for screen, encoder, cmd in combos:
        target = out_dir / f"_probe-{screen}-{encoder}.mp4"
        cmd = [str(target) if part == str(out_dir / "_probe.mp4") else part for part in cmd]
        started = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=seconds + 40)
        except subprocess.TimeoutExpired:
            print(f"{screen:<10}{encoder:<8}{fps:>8}{'超时':>9}")
            continue
        cost = time.time() - started
        if not target.exists() or target.stat().st_size < 10240:
            err = (result.stderr or "").strip().splitlines()
            tail = err[-1][:70] if err else "无输出"
            print(f"{screen:<10}{encoder:<8}{fps:>8}{'失败':>9}  {tail}")
            continue
        size = target.stat().st_size / 1024 / 1024
        actual = real_fps(ffmpeg, target)
        print(f"{screen:<10}{encoder:<8}{fps:>8}{actual:>9.1f}{size:>9.1f}{cost:>8.1f}")


def real_fps(ffmpeg: str, path: Path) -> float:
    probe_bin = find_ffprobe(ffmpeg)
    if not probe_bin:
        return 0.0
    try:
        out = subprocess.run(
            [probe_bin, "-v", "error", "-select_streams", "v:0",
             "-count_packets", "-show_entries", "stream=nb_read_packets,r_frame_rate,duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=40).stdout
        data = json.loads(out)["streams"][0]
        packets = int(data.get("nb_read_packets", 0))
        duration = float(data.get("duration", 0) or 0)
        return packets / duration if duration > 0 else 0.0
    except Exception:
        return 0.0


def verify(ffmpeg: str, path: Path) -> None:
    probe_bin = find_ffprobe(ffmpeg)
    if not probe_bin:
        print("找不到 ffprobe，无法验证")
        return
    out = subprocess.run(
        [probe_bin, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,duration",
         "-show_entries", "format=size,duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=40).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    fmt = data.get("format", {})
    size_mb = int(fmt.get("size", 0)) / 1024 / 1024
    duration = float(stream.get("duration", 0) or fmt.get("duration", 0) or 0)
    frames = int(stream.get("nb_frames", 0) or 0)
    print(f"文件    {path}")
    print(f"分辨率  {stream['width']}x{stream['height']}")
    print(f"标称    {stream['r_frame_rate']} fps  平均 {stream['avg_frame_rate']}")
    print(f"时长    {duration:.1f} 秒   帧数 {frames}   实际 {frames / duration:.2f} fps" if duration else "")
    print(f"体积    {size_mb:.1f} MB   ({size_mb / max(duration, 1) * 60:.0f} MB/分钟)")


def record(ffmpeg: str, fps: int, minutes: float, out: Path, encoder: str, screen: str) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    seconds = int(minutes * 60) if minutes > 0 else None
    cmd = (build(ffmpeg, fps, out, encoder, seconds) if screen == "ddagrab"
           else build_gdi(ffmpeg, fps, out, encoder, seconds))
    meta = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "fps": fps, "encoder": encoder, "screen": screen,
        "minutes": minutes, "out": str(out), "command": " ".join(cmd),
    }
    (out.parent / (out.stem + ".json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"开始录制 -> {out}")
    print(f"规格 {fps} FPS · {encoder} · {screen} · 最长 {minutes} 分钟")
    started = time.time()
    try:
        subprocess.run(cmd, timeout=minutes * 60 + 120 if minutes > 0 else None)
    except KeyboardInterrupt:
        print("\n收到中断，正在收尾…")
    cost = time.time() - started
    if out.exists():
        size = out.stat().st_size / 1024 / 1024
        print(f"\n录制结束：{cost / 60:.1f} 分钟，{size:.0f} MB")
        verify(ffmpeg, out)
        return 0
    print("没有产出文件，检查上面的错误")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--minutes", type=float, default=45)
    parser.add_argument("--encoder", choices=["auto", "qsv", "x264"], default="auto")
    parser.add_argument("--screen", choices=["auto", "ddagrab", "gdigrab"], default="auto")
    parser.add_argument("--out", default="")
    parser.add_argument("--probe", action="store_true", help="只测各种组合的实际帧率")
    parser.add_argument("--verify", default="", help="验证一个已有文件的帧率")
    args = parser.parse_args()

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("找不到 ffmpeg。放到 D:/WBSJ/tools/ffmpeg/bin/ffmpeg.exe 或加进 PATH。")
        return 2
    print(f"ffmpeg  {ffmpeg}")

    if args.verify:
        verify(ffmpeg, Path(args.verify))
        return 0
    if args.probe:
        probe(ffmpeg, seconds=12, fps=args.fps)
        return 0

    # 实测 libx264 更稳（QSV 因为要 hwdownload 再做 bgra->nv12，反而掉帧）
    encoder = "x264" if args.encoder == "auto" else args.encoder
    screen = args.screen
    if screen == "auto":
        screen = "ddagrab"

    out = Path(args.out) if args.out else (
        ROOT / "logs" / "capture"
        / f"screen-{datetime.now().strftime('%m%d-%H%M%S')}.mp4")
    return record(ffmpeg, args.fps, args.minutes, out, encoder, screen)


if __name__ == "__main__":
    sys.exit(main())
