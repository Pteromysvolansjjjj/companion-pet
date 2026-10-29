"""Windows 控制台的中文编码处理。

Windows 控制台默认代码页是 936(GBK)，Python 往里写 UTF-8 中文会变成乱码——
之前 exe 的横幅就是乱码，看起来像「程序坏了」。

两件事：
1. `SetConsoleOutputCP(65001)` 把控制台切到 UTF-8（Windows 10+ 支持）
2. stdout/stderr 改成 UTF-8 输出

老系统切不动就退回 GBK，总比乱码强。
"""

from __future__ import annotations

import sys


def fix_console_encoding() -> str:
    """修好控制台编码，返回实际用的编码名。"""
    encoding = "utf-8"
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if kernel32.SetConsoleOutputCP(65001) == 0:
            encoding = "gbk"
        else:
            kernel32.SetConsoleCP(65001)
    except Exception:
        encoding = "gbk"

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding=encoding, errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    return encoding
