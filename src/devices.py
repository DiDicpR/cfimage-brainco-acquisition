"""硬件扩展边界：真实适配器负责连接、采样和释放资源。"""

import math
from typing import Protocol


class Device(Protocol):
    def open(self) -> None: ...
    def read_sample(self, sample_index: int) -> list[float]: ...
    def close(self) -> None: ...


class MockDevice:
    """确定性正弦信号，用于验证运行和落盘；不代表真实硬件时序。"""

    def __init__(self, sample_rate_hz: int, channels: int):
        self.rate = sample_rate_hz
        self.channels = channels
        self.is_open = False

    def open(self) -> None:
        self.is_open = True

    def read_sample(self, sample_index: int) -> list[float]:
        if not self.is_open:
            raise RuntimeError("设备未打开")
        return [math.sin(2 * math.pi * (channel + 1) * sample_index / self.rate)
                for channel in range(self.channels)]

    def close(self) -> None:
        self.is_open = False
