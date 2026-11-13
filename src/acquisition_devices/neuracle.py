from __future__ import annotations

from pathlib import Path

from .base import (
    DeviceHealth,
    EEGDevice,
    NeuracleApi,
    PendingSync,
    RecordingArtifact,
    SyncPoint,
    TriggerReceipt,
)


class _PendingNeuracleSync:
    def __init__(self, receipt: TriggerReceipt, estimated_sample: int | None) -> None:
        self.receipt = receipt
        self.estimated_sample = estimated_sample


class NeuracleDevice(EEGDevice):
    """Neuracle adapter; TCP parsing and EDF writing stay in the injected API client."""

    def __init__(self, api: NeuracleApi, *, allow_software_trigger_fallback: bool = False) -> None:
        self.api = api
        self.allow_software_trigger_fallback = allow_software_trigger_fallback
        self._hardware_trigger_available: bool | None = None
        self._hardware_trigger_failure_reason: str | None = None

    def connect(self) -> None:
        self._hardware_trigger_available = None
        self._hardware_trigger_failure_reason = None
        self.api.connect()

    def disconnect(self) -> None:
        disconnect = getattr(self.api, "disconnect", None)
        if disconnect is not None:
            disconnect()

    def start_recording(self, output_path: Path | None = None) -> None:
        if output_path is None:
            raise ValueError("Neuracle recording requires an EDF output path.")
        self._hardware_trigger_available = None
        self._hardware_trigger_failure_reason = None
        self.api.start_recording(output_path)

    def stop_recording(self) -> RecordingArtifact:
        return self.api.stop_recording()

    def send_trigger(self, code: int) -> None:
        self.api.send_trigger(code)

    def begin_event(self, code: int, display_timestamp_ns: int) -> SyncPoint | PendingSync:
        estimated_sample = (
            self.api.sample_index() if self.allow_software_trigger_fallback else None
        )
        if self._hardware_trigger_available is False and self.allow_software_trigger_fallback:
            try:
                self.api.send_trigger(code)
            except (RuntimeError, OSError):
                pass
            return self._software_sync(
                estimated_sample,
                failure_reason=self._hardware_trigger_failure_reason,
            )
        try:
            receipt = self.api.send_trigger(code)
        except (RuntimeError, OSError) as error:
            if not self.allow_software_trigger_fallback:
                raise
            self._hardware_trigger_available = False
            self._hardware_trigger_failure_reason = f"hardware_trigger_send_failed: {error}"
            return self._software_sync(
                estimated_sample,
                failure_reason=self._hardware_trigger_failure_reason,
            )
        return PendingSync(_PendingNeuracleSync(receipt, estimated_sample))

    def poll_event(self, pending: PendingSync) -> SyncPoint | None:
        state = pending.handle
        if not isinstance(state, _PendingNeuracleSync):
            raise TypeError("Invalid Neuracle pending trigger handle")
        sync = self.api.poll_trigger(state.receipt)
        if sync is None:
            return None
        return self._resolve_sync(sync, estimated_sample=state.estimated_sample)

    def record_event(self, code: int, display_timestamp_ns: int) -> SyncPoint:
        result = self.begin_event(code, display_timestamp_ns)
        if isinstance(result, SyncPoint):
            return result
        state = result.handle
        if not isinstance(state, _PendingNeuracleSync):
            raise TypeError("Invalid Neuracle pending trigger handle")
        sync = self.api.verify_trigger(state.receipt)
        resolved = self._resolve_sync(sync, estimated_sample=state.estimated_sample)
        if resolved.quality not in {"hardware_verified", "software_estimated"}:
            raise RuntimeError(
                f"Neuracle Trigger {code} was not hardware verified: "
                f"{resolved.failure_reason or 'no matching EEG TRG sample'}；"
                f"未在真实 EEG 的 TRG 通道收到本次标记 {code}，正式实验已停止。")
        return resolved

    def _resolve_sync(self, sync: SyncPoint, *, estimated_sample: int | None) -> SyncPoint:
        if sync.sample_index is not None and sync.quality == "hardware_verified":
            self._hardware_trigger_available = True
            self._hardware_trigger_failure_reason = None
            return sync
        if self.allow_software_trigger_fallback:
            self._hardware_trigger_available = False
            self._hardware_trigger_failure_reason = sync.failure_reason
            fallback_sample = estimated_sample
            if fallback_sample is None:
                fallback_sample = self.api.sample_index()
            return self._software_sync(fallback_sample, failure_reason=sync.failure_reason)
        return sync

    def trigger_self_test(self, code: int) -> SyncPoint:
        return self.record_event(code, 0)

    def health(self) -> DeviceHealth:
        return self.api.health()

    @staticmethod
    def _software_sync(
        sample_index: int | None,
        *,
        failure_reason: str | None = "hardware_trigger_unavailable",
    ) -> SyncPoint:
        if sample_index is None:
            raise RuntimeError("Neuracle software alignment has no current EEG sample index")
        return SyncPoint(
            sample_index,
            "jellyfish_sample_counter",
            "software_estimated",
            failure_reason=failure_reason,
        )
