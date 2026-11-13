from __future__ import annotations

from pathlib import Path

from .base import BrainCoApi, DeviceHealth, EEGDevice, RecordingArtifact, SyncPoint


class BrainCoDevice(EEGDevice):
    """BrainCo adapter using BCIGo SDK data and its continuous EDF recording."""

    def __init__(self, api: BrainCoApi) -> None:
        self.api = api

    def connect(self) -> None:
        self.api.connect()

    def start_recording(self, output_path: Path | None = None) -> None:
        if output_path is None:
            raise ValueError("BrainCo recording requires an EDF output path.")
        self.api.start_recording(output_path)

    def stop_recording(self) -> RecordingArtifact:
        return self.api.stop_recording()

    def send_trigger(self, code: int) -> None:
        # BrainCo has no hardware trigger in this design. Event alignment is
        # recorded locally against the SDK sample counter.
        return None

    def record_event(self, code: int, display_timestamp_ns: int) -> SyncPoint:
        sample_index = self.api.sample_index()
        if sample_index is None:
            return SyncPoint(
                None,
                "sdk_sample_clock",
                "degraded",
                failure_reason="sample_index_unavailable",
            )
        return SyncPoint(sample_index, "software_estimated", "software_estimated")

    def health(self) -> DeviceHealth:
        return self.api.health()
