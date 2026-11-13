from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from PySide6.QtCore import QLockFile


@contextmanager
def acquisition_lock(mode: str, devices: dict) -> Iterator[None]:
    """Reserve a real EEG endpoint across tasks, databases and processes."""
    if mode == "simulation":
        yield
        return
    config = devices.get("devices", {}).get(mode, {})
    endpoint = f"{mode}:{str(config.get('host', '127.0.0.1')).strip().lower()}:{config.get('port', 8712)}"
    key = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    directory = Path(tempfile.gettempdir()) / "things-eeg2-device-locks"
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / (key + ".lock")))
    # Disable age-only stale detection: long EEG recordings legitimately hold
    # the lock for hours. QLockFile still detects dead processes.
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise RuntimeError(
            "脑电设备正被另一版实验或连接检查使用。请先结束并保存当前采集，再启动本版；"
            "单图与双图可分别启动，不能同时占用同一套脑电设备。")
    try:
        yield
    finally:
        lock.unlock()
