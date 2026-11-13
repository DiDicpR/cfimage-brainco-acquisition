from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameClock:
    refresh_rate_hz: float

    @property
    def frame_period_ms(self) -> float:
        return 1000.0 / self.refresh_rate_hz

    def frames_for(self, duration_ms: int) -> int:
        return max(1, round(duration_ms / self.frame_period_ms))

    def duration_for_frames(self, frames: int) -> int:
        return round(frames * self.frame_period_ms)
