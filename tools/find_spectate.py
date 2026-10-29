"""从录制素材里找「观战视角」帧。

判据：正常第一人称视角下，画面左下角是自己的血量/护甲数字；
死亡切观战视角后，同一块区域会变成队友的头像 + 名字（文字）。

所以扫描左下角，读出「非纯数字的文字」就当作候选。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vision"))

import cv2

from lolvision.ocr import OcrEngine

CAP = ROOT / "logs" / "capture"
SCAN = CAP / "_scan"
FFMPEG = "D:/WBSJ/tools/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe"

# 左下角怀疑区域（归一化）。取宽一点，避免头像框刚好卡在边上。
BOX = (0.00, 0.68, 0.30, 1.00)


def extract(video: Path, every_seconds: int = 4) -> list[Path]:
    out_dir = SCAN / video.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("f-*.jpg"))
    if existing:
        return existing
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(video), "-vf", f"fps=1/{every_seconds}",
         str(out_dir / "f-%04d.jpg")],
        timeout=600)
    return sorted(out_dir.glob("f-*.jpg"))


def main() -> int:
    videos = sorted(CAP.glob("hud-*.mp4"))
    # 只看体积大的段（对局画面）
    videos = [v for v in videos if v.stat().st_size / 1024 / 1024 > 50]
    if not videos:
        print("没有够大的对局段")
        return 1

    engine = OcrEngine(min_score=0.25, threads=4)
    engine.ensure()
    engine.warmup()

    print(f"扫描 {len(videos)} 段，左下角区域 x 0-30% y 68-100%\n")
    candidates = []
    for video in videos:
        frames = extract(video)
        hits = 0
        for frame_path in frames:
            image = cv2.imread(str(frame_path))
            if image is None:
                continue
            height, width = image.shape[:2]
            crop = image[int(height * BOX[1]):int(height * BOX[3]),
                         int(width * BOX[0]):int(width * BOX[2])]
            if crop.size == 0:
                continue
            lines = engine.read(crop, mode="line")
            texts = [line.text.strip() for line in lines if line.text.strip()]
            joined = " ".join(texts)
            # 纯数字/符号 = 正常视角的血量；有字母 = 可能是名字
            has_letter = any(ch.isalpha() for ch in joined)
            if has_letter:
                hits += 1
                candidates.append((frame_path, joined, lines))
        print(f"  {video.name:<32} {len(frames):>3} 帧, 命中 {hits}")

    print(f"\n候选 {len(candidates)} 帧：")
    for frame_path, joined, lines in candidates[:20]:
        detail = " | ".join(f"{l.text}({l.score:.2f})" for l in lines[:4])
        print(f"  {frame_path.parent.name}/{frame_path.name}  ->  {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
