from __future__ import annotations

from .base import DeviceHealth, EEGDevice


class LSLDevice(EEGDevice):
    """LSL boundary; install the optional lsl extra before implementing transport."""

    def connect(self) -> None:
        raise NotImplementedError("LSL transport is an optional second-stage integration.")

    def start_recording(self) -> None:
        raise NotImplementedError

    def stop_recording(self) -> None:
        raise NotImplementedError

    def send_trigger(self, code: int) -> None:
        raise NotImplementedError

    def health(self) -> DeviceHealth:
        return DeviceHealth(False, "lsl", "Not configured")
