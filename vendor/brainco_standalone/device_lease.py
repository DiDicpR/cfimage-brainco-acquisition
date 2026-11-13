"""Prevent this kit's CLI and GUI from acquiring the same cap concurrently."""
import os
from pathlib import Path


class DeviceLease:
    def __init__(self):
        self.file = None

    def __enter__(self):
        path = Path(__file__).resolve().parent / '.device.lock'
        self.file = path.open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close(); self.file = None
            raise RuntimeError('本工具包已有采集或调试进程，请先停止该进程。此锁无法替代 BCIGo 的断开操作。')
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close(); self.file = None
