from __future__ import annotations

import time

from .base import EEGDevice, PendingSync, SyncPoint


class TriggerError(RuntimeError):
    pass


class TriggerRouter:
    def __init__(self, device: EEGDevice, codes: dict[str, int]) -> None:
        self.device = device
        self.codes = codes

    def emit(self, event_type: str, *, display_timestamp_ns: int | None = None) -> tuple[int, SyncPoint]:
        code = self.codes[event_type]
        health = self.device.health()
        if not health.connected:
            raise TriggerError(health.message or "Device is disconnected.")
        try:
            sync = self.device.record_event(code, display_timestamp_ns or time.monotonic_ns())
        except Exception as error:
            raise TriggerError(f"{event_type} delivery failed: {error}") from error
        return code, sync

    def emit_deferred(
        self,
        event_type: str,
        *,
        display_timestamp_ns: int | None = None,
    ) -> tuple[int, SyncPoint | PendingSync]:
        """Send now but allow hardware verification to finish without blocking Qt."""

        code = self.codes[event_type]
        health = self.device.health()
        if not health.connected:
            raise TriggerError(health.message or "Device is disconnected.")
        try:
            result = self.device.begin_event(
                code,
                display_timestamp_ns or time.monotonic_ns(),
            )
        except Exception as error:
            raise TriggerError(f"{event_type} delivery failed: {error}") from error
        return code, result

    def poll(self, event_type: str, pending: PendingSync) -> SyncPoint | None:
        try:
            return self.device.poll_event(pending)
        except Exception as error:
            raise TriggerError(f"{event_type} verification failed: {error}") from error
