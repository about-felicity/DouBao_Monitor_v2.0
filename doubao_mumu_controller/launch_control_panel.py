from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import time


BASE_DIR = Path(__file__).resolve().parent
TITLE_MARKER = "批量提问与定时控制台"
SW_RESTORE = 9


def matching_windows() -> list[int]:
    user32 = ctypes.windll.user32
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        if TITLE_MARKER in buffer.value:
            handles.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return handles


def activate(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)


def main() -> int:
    existing = matching_windows()
    if existing:
        activate(existing[0])
        return 0

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(BASE_DIR / "open_control_panel.cmd")],
        cwd=str(BASE_DIR),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        return int(result.returncode)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        windows = matching_windows()
        if windows:
            activate(windows[0])
            return 0
        time.sleep(0.25)
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
