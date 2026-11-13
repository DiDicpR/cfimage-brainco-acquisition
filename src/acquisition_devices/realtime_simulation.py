from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .base import DataGap, DeviceHealth, EEGDevice, RecordingArtifact, SyncPoint
from .edf import EdfWriter, sine_block


class RealtimeSimulatedDevice(EEGDevice):
    """Realtime synthetic source exercising the same artifacts as real EEG."""

    def __init__(self, device_type: str, config: dict[str, Any]) -> None:
        if device_type not in {"neuracle", "brainco"}:
            raise ValueError("Realtime simulation supports neuracle or brainco")
        self.device_type = device_type
        self.sampling_rate_hz = int(config.get("sampling_rate_hz", 500))
        self.channel_count = int(config.get("channel_count", 8))
        self.block_duration_ms = int(config.get("block_duration_ms", 20))
        self.inject_gap_after_blocks = config.get("inject_gap_after_blocks")
        self.inject_gap_samples = int(config.get("inject_gap_samples", 0))
        self.channel_names = tuple(f"EEG{index + 1:03d}" for index in range(self.channel_count))
        if device_type == "neuracle":
            self.channel_names += ("TRG",)
        self.connected = False
        self.recording = False
        self.sample_count = 0
        self._writer: EdfWriter | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._trigger_samples: list[tuple[int, int, threading.Event]] = []
        self._start_ns: int | None = None
        self._end_ns: int | None = None
        self._gaps: list[DataGap] = []

    def connect(self) -> None:
        self.connected = True

    def start_recording(self, output_path: Path | None = None) -> None:
        if not self.connected:
            raise RuntimeError("Synthetic EEG device is not connected")
        if output_path is None:
            raise ValueError("Realtime simulation requires an EDF output path")
        units = tuple("uV" for _ in range(self.channel_count))
        if self.device_type == "neuracle":
            units += ("code",)
        self._writer = EdfWriter(
            output_path,
            channel_names=self.channel_names,
            sampling_rate_hz=self.sampling_rate_hz,
            units=units,
        )
        self.sample_count = 0
        self._gaps.clear()
        self._start_ns = time.monotonic_ns()
        self._stop.clear()
        self.recording = True
        self._thread = threading.Thread(target=self._produce, name=f"synthetic-{self.device_type}", daemon=True)
        self._thread.start()

    def _produce(self) -> None:
        block_samples = max(1, round(self.sampling_rate_hz * self.block_duration_ms / 1000))
        deadline = time.monotonic()
        block_index = 0
        while not self._stop.is_set():
            with self._lock:
                block_index += 1
                if self.inject_gap_after_blocks == block_index and self.inject_gap_samples:
                    gap_start = self.sample_count
                    gap_start_ns = time.monotonic_ns()
                    padding = tuple(tuple(0.0 for _ in range(self.inject_gap_samples)) for _ in self.channel_names)
                    assert self._writer is not None
                    self._writer.write_block(padding)
                    self.sample_count += self.inject_gap_samples
                    self._gaps.append(
                        DataGap(
                            start_sample_index=gap_start,
                            end_sample_index=self.sample_count - 1,
                            missing_samples=self.inject_gap_samples,
                            start_timestamp_ns=gap_start_ns,
                            end_timestamp_ns=gap_start_ns
                            + round(self.inject_gap_samples * 1_000_000_000 / self.sampling_rate_hz),
                            reason="synthetic_injected_gap",
                            recovered=True,
                        )
                    )
                first_sample = self.sample_count
                eeg = sine_block(
                    first_sample=first_sample,
                    sample_count=block_samples,
                    channel_count=self.channel_count,
                    sampling_rate_hz=self.sampling_rate_hz,
                )
                emitted: list[tuple[int, int, threading.Event]] = []
                if self.device_type == "neuracle":
                    trigger = [0.0] * block_samples
                    for code, sample_index, confirmation in self._trigger_samples:
                        offset = sample_index - first_sample
                        if 0 <= offset < block_samples:
                            trigger[offset] = float(code)
                            emitted.append((code, sample_index, confirmation))
                    eeg += (tuple(trigger),)
                assert self._writer is not None
                self._writer.write_block(eeg)
                self.sample_count += block_samples
                for pending in emitted:
                    self._trigger_samples.remove(pending)
                    pending[2].set()
            deadline += self.block_duration_ms / 1000
            self._stop.wait(max(0.0, deadline - time.monotonic()))

    def stop_recording(self) -> RecordingArtifact:
        if not self.recording or self._writer is None:
            raise RuntimeError("Synthetic EEG recording is not running")
        self._stop.set()
        assert self._thread is not None
        self._thread.join()
        with self._lock:
            self._writer.close()
            path = self._writer.path
        self.recording = False
        self._end_ns = time.monotonic_ns()
        return RecordingArtifact(
            path=path,
            source_transport=f"synthetic-{self.device_type}",
            sample_count=self.sample_count,
            sampling_rate_hz=float(self.sampling_rate_hz),
            channel_count=len(self.channel_names),
            channel_names=self.channel_names,
            channel_units=tuple("code" if name == "TRG" else "uV" for name in self.channel_names),
            reference="synthetic-reference",
            recording_start_ns=self._start_ns,
            recording_end_ns=self._end_ns,
            gaps=tuple(self._gaps),
        )

    def send_trigger(self, code: int) -> None:
        if self.device_type == "neuracle":
            with self._lock:
                self._trigger_samples.append((code, self.sample_count, threading.Event()))

    def record_event(self, code: int, display_timestamp_ns: int) -> SyncPoint:
        if not self.recording:
            raise RuntimeError("Synthetic EEG recording is not running")
        with self._lock:
            sample_index = self.sample_count
            if self.device_type == "neuracle":
                confirmation = threading.Event()
                self._trigger_samples.append((code, sample_index, confirmation))
            else:
                confirmation = None
        if confirmation is not None and not confirmation.wait(timeout=1.0):
            raise RuntimeError(f"Synthetic Neuracle Trigger {code} verification timed out")
        quality = "hardware_verified" if self.device_type == "neuracle" else "software_estimated"
        method = "hardware_trigger" if self.device_type == "neuracle" else "sdk_sample_clock"
        eeg_timestamp_ns = (
            self._start_ns + round(sample_index * 1_000_000_000 / self.sampling_rate_hz)
            if self._start_ns
            else None
        )
        return SyncPoint(
            sample_index,
            method,
            quality,
            eeg_timestamp_ns=eeg_timestamp_ns,
            latency_ns=None if eeg_timestamp_ns is None else eeg_timestamp_ns - display_timestamp_ns,
        )

    def trigger_self_test(self, code: int) -> SyncPoint:
        sync = self.record_event(code, time.monotonic_ns())
        if self.device_type == "neuracle" and sync.quality != "hardware_verified":
            raise RuntimeError("Synthetic Neuracle Trigger self-test failed")
        return sync

    def health(self) -> DeviceHealth:
        return DeviceHealth(
            connected=self.connected,
            device_name=self.device_type,
            message="synthetic realtime EEG",
            sampling_rate_hz=float(self.sampling_rate_hz),
            channel_count=len(self.channel_names),
            recording=self.recording,
            state="recording" if self.recording else "ready",
            serial_number=f"SIM-{self.device_type.upper()}",
            sample_count=self.sample_count,
            last_sample_index=self.sample_count - 1 if self.sample_count else None,
            last_block_timestamp_ns=time.monotonic_ns() if self.recording else self._end_ns,
            dropped_block_count=len(self._gaps),
            gap_sample_count=sum(gap.missing_samples for gap in self._gaps),
        )


def create_realtime_simulated_device(config: dict[str, Any]) -> EEGDevice:
    return RealtimeSimulatedDevice(str(config["device_type"]), config)
