from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DeviceHealth:
    connected: bool
    device_name: str
    message: str = ""
    sampling_rate_hz: float | None = None
    channel_count: int | None = None
    recording: bool = False
    state: str = "ready"
    serial_number: str | None = None
    sample_count: int = 0
    last_sample_index: int | None = None
    last_block_timestamp_ns: int | None = None
    dropped_block_count: int = 0
    gap_sample_count: int = 0
    reconnect_count: int = 0


@dataclass(frozen=True)
class SyncPoint:
    sample_index: int | None
    alignment_method: str
    quality: str
    eeg_timestamp_ns: int | None = None
    latency_ns: int | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class TriggerReceipt:
    """Identity and send time for one physical trigger command."""

    code: int
    sequence: int
    sent_timestamp_ns: int


@dataclass(frozen=True)
class PendingSync:
    """Opaque device-owned handle for non-blocking EEG event verification."""

    handle: object


@dataclass(frozen=True)
class DataGap:
    start_sample_index: int
    end_sample_index: int
    missing_samples: int
    start_timestamp_ns: int
    end_timestamp_ns: int
    reason: str
    recovered: bool = False


@dataclass(frozen=True)
class RecordingArtifact:
    path: Path
    source_transport: str
    sample_count: int | None = None
    sampling_rate_hz: float | None = None
    channel_count: int | None = None
    channel_names: tuple[str, ...] = ()
    channel_units: tuple[str, ...] = ()
    reference: str | None = None
    recording_start_ns: int | None = None
    recording_end_ns: int | None = None
    gaps: tuple[DataGap, ...] = ()


class EEGDevice(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def start_recording(self, output_path: Path | None = None) -> None: ...

    @abstractmethod
    def stop_recording(self) -> RecordingArtifact | None: ...

    @abstractmethod
    def send_trigger(self, code: int) -> None: ...

    @abstractmethod
    def health(self) -> DeviceHealth: ...

    def trigger_self_test(self, code: int) -> SyncPoint:
        return self.record_event(code, 0)

    def record_event(self, code: int, display_timestamp_ns: int) -> SyncPoint:
        """Record a stimulus event and return its EEG alignment when available."""
        self.send_trigger(code)
        return SyncPoint(None, "not_available", "degraded", failure_reason="sample_index_unavailable")

    def begin_event(self, code: int, display_timestamp_ns: int) -> SyncPoint | PendingSync:
        """Send an event, optionally deferring slow hardware verification."""

        return self.record_event(code, display_timestamp_ns)

    def poll_event(self, pending: PendingSync) -> SyncPoint | None:
        """Return a deferred alignment, or ``None`` while it is still pending."""

        raise RuntimeError("This EEG device does not support deferred event verification")


class NeuracleApi(Protocol):
    """Site-specific wrapper around Neuracle realtime TCP and TriggerBox APIs."""

    def connect(self) -> None: ...

    def start_recording(self, output_path: Path) -> None: ...

    def stop_recording(self) -> RecordingArtifact: ...

    def send_trigger(self, code: int) -> TriggerReceipt: ...

    def poll_trigger(self, receipt: TriggerReceipt) -> SyncPoint | None: ...

    def verify_trigger(self, receipt: TriggerReceipt) -> SyncPoint: ...

    def sample_index(self) -> int | None: ...

    def health(self) -> DeviceHealth: ...


class BrainCoApi(Protocol):
    """Small wrapper around the BCIGo SDK used by the experiment runner."""

    def connect(self) -> None: ...

    def start_recording(self, output_path: Path) -> None: ...

    def stop_recording(self) -> RecordingArtifact: ...

    def sample_index(self) -> int | None: ...

    def health(self) -> DeviceHealth: ...
