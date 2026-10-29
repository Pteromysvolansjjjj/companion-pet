@echo off
chcp 65001 >nul
title 录屏 - 按 Ctrl+C 或关窗口停止
cd /d "%~dp0"

set FF=D:\WBSJ\tools\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe
set OUT=logs\capture

if not exist "%FF%" (
  echo [!] 找不到 ffmpeg: %FF%
  pause
  exit /b 1
)
if not exist "%OUT%" mkdir "%OUT%"

echo ============================================================
echo   录屏  2560x1440  20fps  每 2 分钟存一段
echo   输出目录: %CD%\%OUT%
echo   停止: 按 Ctrl+C，或直接关掉这个窗口
echo   （分段存储，强杀也只丢最后一段，前面的都能用）
echo ============================================================
echo.

"%FF%" -y -hide_banner -loglevel warning -stats ^
 -f lavfi -i "ddagrab=framerate=20" ^
 -vf "hwdownload,format=bgra" ^
 -c:v libx264 -preset ultrafast -crf 23 ^
 -f segment -segment_time 120 -reset_timestamps 1 -strftime 1 ^
 "%OUT%\hud-%%Y%%m%%d-%%H%%M%%S.mp4"

echo.
echo 录制已结束，文件在 %OUT%
pause
