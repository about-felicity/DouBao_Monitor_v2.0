from __future__ import annotations

import ctypes
import hashlib
import os
import time
from contextlib import contextmanager
from typing import Callable, Iterator


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    _kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    _kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    _kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    _kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)


def _mutex_name(serial: str) -> str:
    digest = hashlib.sha256(str(serial).encode("utf-8")).hexdigest()[:20]
    return rf"Local\ModelMonitor_MuMu_{digest}"


@contextmanager
def device_session(
    serial: str,
    model: str,
    *,
    timeout: float = 900,
    on_wait: Callable[[str], None] | None = None,
) -> Iterator[None]:
    """Exclusively own one emulator while an App question is in progress.

    All local model collectors are separate processes. A Windows named mutex is
    therefore used instead of a threading lock; Windows releases it if a process
    crashes, so a stale lock cannot permanently stop collection.
    """
    if os.name != "nt":
        yield
        return
    handle = _kernel32.CreateMutexW(None, False, _mutex_name(serial))
    if not handle:
        raise OSError(ctypes.get_last_error(), "cannot create MuMu device mutex")
    acquired = False
    try:
        deadline = time.monotonic() + max(1.0, timeout)
        announced = False
        while time.monotonic() < deadline:
            result = _kernel32.WaitForSingleObject(handle, 1000)
            if result in (0x00000000, 0x00000080):  # acquired / abandoned owner
                acquired = True
                break
            if result != 0x00000102:
                raise OSError(ctypes.get_last_error(), "waiting for MuMu device mutex failed")
            if on_wait and not announced:
                on_wait(f"{model} 正在排队等待 MuMu {serial}，不会与其他模型抢占模拟器")
                announced = True
        if not acquired:
            raise TimeoutError(f"{model} 等待 MuMu {serial} 使用权超时")
        yield
    finally:
        if acquired:
            _kernel32.ReleaseMutex(handle)
        _kernel32.CloseHandle(handle)
