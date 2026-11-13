from __future__ import annotations

import asyncio
import importlib
import inspect
import threading
import time
from pathlib import Path
from typing import Any

from .base import DeviceHealth, RecordingArtifact
from .brainco import BrainCoDevice


class BCIGoSdkApi:
    """Concrete ``BrainCoApi`` backed by the public ``bcigo_sdk`` package.

    BCIGo owns continuous acquisition and EDF writing.  The experiment only
    snapshots the SDK data callback counter, so its event locations are always
    software estimates rather than hardware-trigger confirmations.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._sdk: Any | None = None
        self._client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._lock = threading.Lock()
        self._connected = False
        self._recording = False
        self._sample_index = 0
        self._recording_path: Path | None = None
        self._recording_started_ns: int | None = None
        self._last_block_timestamp_ns: int | None = None
        self._error = ""

    def connect(self) -> None:
        if self._connected:
            return
        try:
            self._sdk = importlib.import_module("bcigo_sdk")
        except ModuleNotFoundError as error:
            raise RuntimeError("BCIGo SDK is not installed; install the project brainco extra.") from error
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 0))
        if not host or port < 1:
            raise ValueError("BrainCo requires devices.brainco.host and devices.brainco.port")
        self._start_event_loop()
        try:
            self._client = self._sdk.BCIGoClient(host, port)
            parser = self._sdk.MessageParser("bcigo", self._sdk.MsgType.BCIGo)
            self._call_async(
                self._client.start_stream(
                    parser,
                    fs=self._sample_rate_enum(),
                    gain=self._gain_enum(),
                    signal=self._signal_enum(),
                )
            )
            self._sdk.set_eeg_data_callback(self._on_eeg_data)
            self._connected = True
        except Exception:
            self._disconnect()
            raise

    def start_recording(self, output_path: Path) -> None:
        if not self._connected or self._sdk is None:
            raise RuntimeError("BrainCo device is not connected")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        participant_code = str(self.config.get("participant_code") or output_path.name.split("_", 1)[0])
        participant_name = str(self.config.get("participant_name") or participant_code)
        result = self._sdk.start_edf_recording(
            str(output_path.parent),
            participant_code,
            str(self.config.get("participant_sex", "X")),
            str(self.config.get("participant_birthdate", "01-JAN-1900")),
            participant_name,
        )
        path = Path(str(result))
        self._recording_path = path if path.is_absolute() else output_path.parent / path
        with self._lock:
            self._sample_index = 0
            self._recording_started_ns = time.monotonic_ns()
            self._last_block_timestamp_ns = None
            self._recording = True

    def stop_recording(self) -> RecordingArtifact:
        if self._sdk is None or self._recording_path is None:
            raise RuntimeError("BrainCo recording was not started")
        with self._lock:
            self._recording = False
            sample_count = self._sample_index
            started_ns = self._recording_started_ns
        path = self._recording_path
        self._recording_path = None
        try:
            self._sdk.stop_edf_recording()
        finally:
            self._disconnect()
        if not path.is_file():
            raise FileNotFoundError(f"BCIGo SDK did not write the EDF recording: {path}")
        return RecordingArtifact(
            path=path,
            source_transport="bcigo-sdk",
            sample_count=sample_count,
            sampling_rate_hz=float(self.config.get("sampling_rate_hz", 250)),
            channel_count=int(self.config.get("channel_count", 32)),
            recording_start_ns=started_ns,
            recording_end_ns=time.monotonic_ns(),
        )

    def sample_index(self) -> int | None:
        with self._lock:
            return self._sample_index if self._recording and self._sample_index > 0 else None

    def health(self) -> DeviceHealth:
        with self._lock:
            sample_index = self._sample_index if self._recording else None
            last_block = self._last_block_timestamp_ns
            recording = self._recording
        state = "recording" if recording else "ready"
        return DeviceHealth(
            connected=self._connected and not self._error,
            device_name="brainco-bcigo",
            message=self._error,
            sampling_rate_hz=float(self.config.get("sampling_rate_hz", 250)),
            channel_count=int(self.config.get("channel_count", 32)),
            recording=recording,
            state="error" if self._error else state,
            last_sample_index=sample_index,
            last_block_timestamp_ns=last_block,
        )

    def _on_eeg_data(self, *payload: Any) -> None:
        """Count SDK callback batches without making assumptions about EEG values."""

        with self._lock:
            if not self._recording:
                return
            self._sample_index += _samples_in_callback(payload)
            self._last_block_timestamp_ns = time.monotonic_ns()

    def _start_event_loop(self) -> None:
        if self._loop is not None:
            return

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            loop.run_forever()
            loop.close()

        self._loop_thread = threading.Thread(target=run_loop, name="bcigo-sdk", daemon=True)
        self._loop_thread.start()
        if not self._loop_ready.wait(timeout=5) or self._loop is None:
            raise RuntimeError("Could not start the BCIGo SDK event loop")

    def _call_async(self, value: Any) -> Any:
        async def resolve() -> Any:
            return await value if inspect.isawaitable(value) else value

        if self._loop is None:
            raise RuntimeError("BCIGo SDK event loop is not available")
        try:
            return asyncio.run_coroutine_threadsafe(resolve(), self._loop).result(timeout=15)
        except Exception as error:
            self._error = str(error)
            raise RuntimeError(f"BCIGo SDK operation failed: {error}") from error

    def _disconnect(self) -> None:
        if self._sdk is not None:
            self._sdk.set_eeg_data_callback(None)
        if self._client is not None:
            if hasattr(self._client, "disconnect_tcp_blocking"):
                self._client.disconnect_tcp_blocking()
            else:
                self._call_async(self._client.disconnect_tcp())
        self._connected = False
        self._client = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
        self._loop = None
        self._loop_thread = None
        self._loop_ready.clear()

    def _sample_rate_enum(self) -> Any:
        return _enum_value(self._sdk.EegSampleRate, f"SR_{int(self.config.get('sampling_rate_hz', 250))}Hz")

    def _gain_enum(self) -> Any:
        return _enum_value(self._sdk.EegSignalGain, f"GAIN_{int(self.config.get('gain', 6))}")

    def _signal_enum(self) -> Any:
        return _enum_value(self._sdk.EegSignalSource, str(self.config.get("signal", "NORMAL")).upper())


def _enum_value(enum_type: Any, name: str) -> Any:
    try:
        return getattr(enum_type, name)
    except AttributeError as error:
        raise ValueError(f"Unsupported BCIGo SDK option: {name}") from error


def _samples_in_callback(payload: tuple[Any, ...]) -> int:
    """Use an explicit count when provided; otherwise one callback is one sample frame."""

    for value in payload:
        if isinstance(value, dict) and isinstance(value.get("sample_count"), int):
            return max(1, int(value["sample_count"]))
        count = getattr(value, "sample_count", None)
        if isinstance(count, int):
            return max(1, count)
    return 1


def create_device(config: dict[str, Any]) -> BrainCoDevice:
    """Adapter factory for ``devices.brainco.adapter_factory``."""

    return BrainCoDevice(BCIGoSdkApi(config))
