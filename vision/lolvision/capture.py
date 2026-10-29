"""Windows game-window discovery and frame grabbing.

The sidecar never touches the game process: it only asks Windows for the
window rectangle and copies pixels from the desktop compositor (BitBlt through
mss).  No injection, no hooks, no memory reads.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

GAME_CLASSES = {"RiotWindowClass"}
CLIENT_CLASSES = {"RCLIENT", "RiotClientMain"}
TITLE_HINTS = ("league of legends", "英雄联盟")


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def enable_dpi_awareness() -> None:
    """Make GetWindowRect/BitBlt agree with physical pixels on scaled displays."""
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return origin.x, origin.y, width, height


@dataclass
class GameWindow:
    hwnd: int
    title: str
    class_name: str
    x: int
    y: int
    width: int
    height: int
    foreground: bool
    minimized: bool
    kind: str  # "game" | "client"

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "className": self.class_name,
            "width": self.width,
            "height": self.height,
            "x": self.x,
            "y": self.y,
            "foreground": self.foreground,
            "minimized": self.minimized,
            "kind": self.kind,
        }


def find_windows() -> list[GameWindow]:
    """Return visible League windows, game client first."""
    foreground = user32.GetForegroundWindow()
    found: list[GameWindow] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = _class_name(hwnd)
        title = _title(hwnd)
        lowered = title.lower()
        if cls in GAME_CLASSES:
            kind = "game"
        elif cls in CLIENT_CLASSES:
            kind = "client"
        elif any(hint in lowered for hint in TITLE_HINTS):
            kind = "game"
        else:
            return True
        rect = _client_rect(hwnd)
        if not rect:
            return True
        x, y, w, h = rect
        found.append(GameWindow(
            hwnd=int(hwnd), title=title, class_name=cls, x=x, y=y, width=w, height=h,
            foreground=(int(hwnd) == int(foreground)),
            minimized=bool(user32.IsIconic(hwnd)), kind=kind,
        ))
        return True

    user32.EnumWindows(callback, 0)
    found.sort(key=lambda item: (item.kind != "game", not item.foreground, -item.width * item.height))
    return found


def select_window(prefer_foreground: bool = True) -> GameWindow | None:
    windows = [w for w in find_windows() if w.kind == "game" and not w.minimized]
    if not windows:
        return None
    if prefer_foreground:
        for window in windows:
            if window.foreground:
                return window
        return None
    return windows[0]


class Grabber:
    """Screen grabber bound to one mss instance (cheap to reuse)."""

    def __init__(self) -> None:
        import mss

        self._mss = mss.mss()

    def grab(self, x: int, y: int, width: int, height: int) -> np.ndarray:
        shot = self._mss.grab({"left": x, "top": y, "width": width, "height": height})
        frame = np.frombuffer(shot.rgb, dtype=np.uint8).reshape((height, width, 3))
        # mss returns RGB; OpenCV/RapidOCR expect BGR.
        return np.ascontiguousarray(frame[:, :, ::-1])

    def close(self) -> None:
        try:
            self._mss.close()
        except Exception:
            pass
