from __future__ import annotations

from pathlib import Path

from .base import DeviceHealth, EEGDevice, RecordingArtifact


class SimulationDevice(EEGDevice):
    def __init__(self) -> None:
        self.recording = False
        self.triggers: list[int] = []

    def connect(self) -> None:
        return None

    def start_recording(self, output_path: Path | None = None) -> None:
        self.recording = True

    def stop_recording(self) -> RecordingArtifact | None:
        self.recording = False
        return None

    def send_trigger(self, code: int) -> None:
        self.triggers.append(code)

    def health(self) -> DeviceHealth:
        return DeviceHealth(True, "simulation", "No EEG waveform is acquired.")
