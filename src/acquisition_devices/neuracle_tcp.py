from __future__ import annotations

import socket
import select
import json
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import DataGap, DeviceHealth, RecordingArtifact, SyncPoint, TriggerReceipt
from .edf import EdfWriter
from .neuracle import NeuracleDevice


class _TcpDcpTransport:
    """Small serial-like wrapper around a TriggerBox DCP TCP connection."""

    def __init__(self, handle: Any, timeout: float) -> None:
        self._handle = handle
        self._timeout = timeout
        self._closed = False
        if hasattr(handle, "settimeout"):
            handle.settimeout(timeout)

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._timeout = value
        if hasattr(self._handle, "settimeout"):
            self._handle.settimeout(value)

    @property
    def is_open(self) -> bool:
        return not self._closed

    def reset_input_buffer(self) -> None:
        # DCP over TCP is request/reply and every reply is consumed in full.
        return None

    def write(self, payload: bytes) -> int:
        self._handle.sendall(payload)
        return len(payload)

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            try:
                chunk = self._handle.recv(size - len(result))
            except TimeoutError:
                break
            if not chunk:
                break
            result.extend(chunk)
        return bytes(result)

    def discard_pending_replies(self) -> None:
        """Drain optional DCP replies without waiting or delaying a stimulus."""
        if not hasattr(self._handle, "fileno"):
            return
        # Bound the work even if a peer streams unsolicited data indefinitely.
        for _ in range(4):
            readable, _, _ = select.select([self._handle], [], [], 0)
            if not readable:
                return
            if not self._handle.recv(4096):
                raise ConnectionError("TriggerBox TCP connection closed")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._handle.close()


class NeuracleTcpApi:
    """Neuracle JellyFish receiver with serial or TCP DCP trigger output.

    JellyFish wraps channel-major float32 blocks in metadata/data packets.  The
    forwarded stream can contain more channels than the experiment records, so
    metadata channel names are used to select the configured EEG channels and
    ``Trigger``/``TRG`` channel.  A trigger is accepted only after its value
    appears in that forwarded channel.
    """

    _TRIGGER_FUNCTION_ID = 225
    _TRIGGER_DEVICE_ID = 1
    _META_HEAD = bytes.fromhex("5FF5")
    _DATA_HEAD = bytes.fromhex("5AA5")
    _META_TAIL = bytes.fromhex("F55F")
    _DATA_TAIL = bytes.fromhex("A55A")
    _MAX_PACKET_BYTES = 64 * 1024 * 1024

    def __init__(
        self,
        config: dict[str, Any],
        *,
        socket_factory: Callable[..., Any] = socket.create_connection,
        serial_factory: Callable[..., Any] | None = None,
        trigger_socket_factory: Callable[..., Any] = socket.create_connection,
    ) -> None:
        self.config = config
        self._socket_factory = socket_factory
        self._serial_factory = serial_factory
        self._trigger_socket_factory = trigger_socket_factory
        self._socket: Any | None = None
        self._serial: Any | None = None
        self._reader: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._metadata_ready = threading.Event()
        self._condition = threading.Condition(threading.RLock())
        self._serial_lock = threading.Lock()
        self._rx_buffer = b""
        self._stream_meta: dict[str, Any] | None = None
        self._main_serial_number: int | None = None
        self._selected_stream_indices: tuple[int, ...] = ()
        self._forwarded_channel_count = 0
        self._module_name = ""
        self._serial_number: str | None = None
        self._writer: EdfWriter | None = None
        self._raw_handle: Any | None = None
        self._recording_path: Path | None = None
        self._recording_started_ns: int | None = None
        self._recording = False
        self._connected = False
        self._sample_index = 0
        self._last_block_timestamp_ns: int | None = None
        self._trigger_events: deque[tuple[int, int, int]] = deque(maxlen=4096)
        self._trigger_sequence = 0
        self._last_trigger_receipts: dict[int, TriggerReceipt] = {}
        self._trigger_sample_boundaries: dict[int, int] = {}
        self._trigger_results: dict[int, SyncPoint] = {}
        self._ambiguous_trigger_codes: set[int] = set()
        self._gaps: list[DataGap] = []
        self._error = ""
        self._trigger_error = ""
        self._dropped_blocks = 0

    def connect(self) -> None:
        if self._connected:
            return
        self._validate_config()
        host = str(self.config["host"])
        port = int(self.config["port"])
        timeout = float(self.config.get("connect_timeout_sec", 5))
        self._metadata_ready.clear()
        self._rx_buffer = b""
        self._stream_meta = None
        self._main_serial_number = None
        self._selected_stream_indices = ()
        try:
            self._socket = self._socket_factory((host, port), timeout=timeout)
            if hasattr(self._socket, "settimeout"):
                self._socket.settimeout(1.0)
            try:
                self._serial = self._open_trigger_transport(timeout)
                self._validate_trigger_device()
                self._trigger_error = ""
            except Exception as error:
                self._close_trigger_transport()
                if not bool(self.config.get("allow_software_trigger_fallback", False)):
                    raise
                self._trigger_error = str(error)
        except Exception as error:
            self._close_io()
            self._error = str(error)
            raise RuntimeError(f"Neuracle connection failed: {error}") from error
        self._error = ""
        self._connected = True
        self._stop_reader.clear()
        self._reader = threading.Thread(target=self._read_loop, name="neuracle-tcp", daemon=True)
        self._reader.start()
        if not self._metadata_ready.wait(timeout=timeout):
            reason = self._error or (
                f"JellyFish at {host}:{port} did not send stream metadata within {timeout:g}s"
            )
            self._shutdown_reader()
            self._error = reason
            raise RuntimeError(f"Neuracle connection failed: {reason}")
        if not self._connected or self._stream_meta is None:
            reason = self._error or "JellyFish stream metadata could not be parsed"
            self._shutdown_reader()
            self._error = reason
            raise RuntimeError(f"Neuracle connection failed: {reason}")

    def start_recording(self, output_path: Path) -> None:
        if not self._connected:
            raise RuntimeError("Neuracle device is not connected")
        channel_names = self._channel_names()
        units = tuple(str(value) for value in self.config.get("channel_units", ())) or None
        with self._condition:
            if self._recording:
                raise RuntimeError("Neuracle recording is already active")
            self._writer = EdfWriter(
                output_path,
                channel_names=channel_names,
                sampling_rate_hz=int(self.config["sampling_rate_hz"]),
                units=units,
            )
            self._recording_path = output_path
            # Preserve the forwarded float32 values, including DC offsets that
            # cannot fit in the legacy 16-bit EDF convenience export.
            self._raw_handle = output_path.with_suffix('.float32').open('xb')
            self._recording_started_ns = time.monotonic_ns()
            self._sample_index = 0
            self._last_block_timestamp_ns = None
            self._trigger_events.clear()
            self._trigger_sequence = 0
            self._last_trigger_receipts.clear()
            self._trigger_sample_boundaries.clear()
            self._trigger_results.clear()
            self._ambiguous_trigger_codes.clear()
            self._gaps.clear()
            self._recording = True
            self._write_raw_metadata(output_path, complete=False)

    def disconnect(self) -> None:
        """Release a connected, recording or partially started adapter."""
        try:
            if self._recording:
                self.stop_recording()
        finally:
            try:
                if self._writer is not None:
                    self._writer.close()
            finally:
                self._writer = None
                try:
                    if self._raw_handle is not None:
                        self._raw_handle.close()
                finally:
                    self._raw_handle = None
                    self._recording = False
                    self._shutdown_reader()

    def _write_raw_metadata(self, path: Path, *, complete: bool) -> None:
        payload = dict(
            format='little-endian float32', dtype='<f4', layout='sample_major',
            shape=[self._sample_index, int(self.config['channel_count'])],
            sampling_rate_hz=int(self.config['sampling_rate_hz']),
            channel_names=list(self._channel_names()),
            channel_units=list(self.config.get('channel_units', ())),
            trigger_channel='TRG', event_sample_index_base=1,
            raw_file=path.with_suffix('.float32').name,
            recording_start_monotonic_ns=self._recording_started_ns,
            complete=complete,
            edf_note='EDF rounds to integer uV and clips to [-32768,32767]; use float32 for lossless analysis.',
        )
        path.with_suffix('.float32.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def stop_recording(self) -> RecordingArtifact:
        with self._condition:
            if not self._recording or self._writer is None or self._recording_path is None:
                raise RuntimeError("Neuracle recording was not started")
            writer = self._writer
            path = self._recording_path
            sample_count = self._sample_index
            started_ns = self._recording_started_ns
            self._writer = None
            self._recording_path = None
            self._recording = False
        try:
            writer.close()
            if self._raw_handle is not None:
                self._raw_handle.flush()
                self._raw_handle.close()
                self._raw_handle = None
            self._write_raw_metadata(path, complete=True)
        finally:
            if self._raw_handle is not None:
                self._raw_handle.close()
                self._raw_handle = None
            self._shutdown_reader()
        return RecordingArtifact(
            path=path,
            source_transport=(
                "neuracle-jellyfish-tcp-trigger-"
                f"{str(self.config.get('trigger_transport', 'serial')).lower()}"
            ),
            sample_count=sample_count,
            sampling_rate_hz=float(self.config["sampling_rate_hz"]),
            channel_count=int(self.config["channel_count"]),
            channel_names=self._channel_names(),
            channel_units=tuple(str(value) for value in self.config.get("channel_units", ())),
            recording_start_ns=started_ns,
            recording_end_ns=time.monotonic_ns(),
            gaps=tuple(self._gaps),
        )

    def send_trigger(self, code: int) -> TriggerReceipt:
        if not 0 <= code <= 255:
            raise ValueError("Neuracle TriggerBox event codes must be between 0 and 255")
        with self._condition:
            if not self._connected or self._serial is None:
                detail = f": {self._trigger_error}" if self._trigger_error else ""
                raise RuntimeError(f"Neuracle trigger output is not connected{detail}")
        kind = str(self.config.get("trigger_device", "triggerin")).lower()
        with self._serial_lock:
            if self._uses_trg_acknowledgement():
                assert isinstance(self._serial, _TcpDcpTransport)
                self._serial.discard_pending_replies()
            with self._condition:
                if code in self._ambiguous_trigger_codes:
                    raise RuntimeError(
                        f"Trigger {code} previously timed out; restart acquisition and preflight "
                        "before reusing this code to avoid matching a late marker to a new trial"
                    )
                sample_boundary = self._sample_index
            if kind == "triggerbox" and self._uses_trg_acknowledgement():
                # Emit the vendor E1 command once. Only an observed TRG sample
                # can verify this receipt; TCP send success is not verification.
                assert self._serial is not None
                previous_timeout = self._serial.timeout
                self._serial.timeout = max(0.01, float(
                    self.config.get("trigger_command_timeout_ms", 250)) / 1000)
                try:
                    sent_ns = time.monotonic_ns()
                    self._write_trigger_frame(self._serial, struct.pack(
                        "<BBHB", self._TRIGGER_DEVICE_ID, self._TRIGGER_FUNCTION_ID, 1, code))
                finally:
                    self._serial.timeout = previous_timeout
            elif kind == "triggerbox":
                payload, sent_ns = self._triggerbox_transaction(
                    self._TRIGGER_FUNCTION_ID,
                    bytes((code,)),
                    timeout_sec=float(
                        self.config.get(
                            "trigger_command_timeout_ms",
                            self.config.get("trigger_timeout_ms", 250),
                        )
                    )
                    / 1000,
                )
                # TriggerBox firmware variants acknowledge E1 either with the
                # function byte (E1) or by echoing the emitted event code.
                if len(payload) != 1 or payload[0] not in {
                    self._TRIGGER_FUNCTION_ID,
                    code,
                }:
                    raise RuntimeError(
                        f"Neuracle TriggerBox rejected event code {code}: "
                        f"unexpected acknowledgement {payload.hex(' ') or '<empty>'}"
                    )
            else:
                assert self._serial is not None
                if hasattr(self._serial, "reset_input_buffer"):
                    self._serial.reset_input_buffer()
                frame = struct.pack(
                    "<BBHB", self._TRIGGER_DEVICE_ID, self._TRIGGER_FUNCTION_ID, 1, code
                )
                sent_ns = time.monotonic_ns()
                self._write_trigger_frame(self._serial, frame)
                if hasattr(self._serial, "flush"):
                    self._serial.flush()
        with self._condition:
            self._trigger_sequence += 1
            receipt = TriggerReceipt(code, self._trigger_sequence, sent_ns)
            self._last_trigger_receipts[code] = receipt
            self._trigger_sample_boundaries[receipt.sequence] = sample_boundary
            return receipt

    def poll_trigger(self, receipt: TriggerReceipt) -> SyncPoint | None:
        with self._condition:
            return self._poll_trigger_locked(receipt)

    def verify_trigger(self, receipt: TriggerReceipt | int) -> SyncPoint:
        if isinstance(receipt, int):
            receipt = self._last_trigger_receipts.get(
                receipt,
                TriggerReceipt(receipt, 0, time.monotonic_ns()),
            )
        deadline_ns = receipt.sent_timestamp_ns + self._verification_timeout_ns()
        with self._condition:
            while True:
                sync = self._poll_trigger_locked(receipt)
                if sync is not None:
                    return sync
                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    # The next loop converts the expired receipt to a degraded result.
                    continue
                self._condition.wait(timeout=remaining_ns / 1_000_000_000)

    def _poll_trigger_locked(self, receipt: TriggerReceipt) -> SyncPoint | None:
        if receipt.sequence in self._trigger_results:
            return self._trigger_results[receipt.sequence]
        boundary = self._trigger_sample_boundaries.get(receipt.sequence, 0)
        for event in self._trigger_events:
            event_code, sample_index, observed_ns = event
            if (event_code == receipt.code and sample_index > boundary
                    and observed_ns >= receipt.sent_timestamp_ns):
                # A forwarded TRG sample may satisfy only one physical send.
                self._trigger_events.remove(event)
                result = SyncPoint(
                    sample_index=sample_index,
                    alignment_method="tcp_trg_channel",
                    quality="hardware_verified",
                    eeg_timestamp_ns=observed_ns,
                    latency_ns=max(0, observed_ns - receipt.sent_timestamp_ns),
                )
                self._remember_trigger_result(receipt, result)
                return result
        if not self._connected:
            return SyncPoint(
                None,
                "tcp_trg_channel",
                "degraded",
                failure_reason=self._error or "tcp_disconnected",
            )
        if time.monotonic_ns() >= receipt.sent_timestamp_ns + self._verification_timeout_ns():
            self._ambiguous_trigger_codes.add(receipt.code)
            result = SyncPoint(
                None,
                "tcp_trg_channel",
                "degraded",
                failure_reason=(
                    f"trigger_{receipt.code}_sequence_{receipt.sequence}_"
                    "not_seen_in_trg_channel; " + json.dumps(dict(
                        sent_timestamp_ns=receipt.sent_timestamp_ns,
                        sample_boundary=boundary, received_samples=self._sample_index,
                        stream_age_ms=((time.monotonic_ns()-self._last_block_timestamp_ns)/1e6
                            if self._last_block_timestamp_ns is not None else None),
                        recent_unmatched_markers=[list(e[:2]) for e in list(self._trigger_events)[-8:]],
                        action='stop_without_resend'), separators=(',',':'))
                ),
            )
            self._remember_trigger_result(receipt, result)
            return result
        return None

    def _remember_trigger_result(self, receipt: TriggerReceipt, result: SyncPoint) -> None:
        self._trigger_sample_boundaries.pop(receipt.sequence, None)
        self._trigger_results[receipt.sequence] = result
        if len(self._trigger_results) > 4096:
            self._trigger_results.pop(next(iter(self._trigger_results)))

    def _verification_timeout_ns(self) -> int:
        timeout_ms = float(
            self.config.get(
                "trigger_verification_timeout_ms",
                self.config.get("trigger_timeout_ms", 250),
            )
        )
        return max(1, round(timeout_ms * 1_000_000))

    def health(self) -> DeviceHealth:
        with self._condition:
            recording = self._recording
            sample_index = self._sample_index if recording else None
            last_block = self._last_block_timestamp_ns
            error = self._error
        return DeviceHealth(
            connected=self._connected and not error,
            device_name="neuracle-jellyfish-tcp-trigger",
            message=error
            or (
                f"hardware trigger unavailable: {self._trigger_error}"
                if self._trigger_error
                else ""
            ),
            sampling_rate_hz=float(self.config["sampling_rate_hz"]),
            channel_count=int(self.config["channel_count"]),
            serial_number=self._serial_number,
            recording=recording,
            state="error" if error else ("recording" if recording else "ready"),
            sample_count=self._sample_index,
            last_sample_index=sample_index,
            last_block_timestamp_ns=last_block,
            dropped_block_count=self._dropped_blocks,
            gap_sample_count=sum(gap.missing_samples for gap in self._gaps),
        )

    def sample_index(self) -> int | None:
        """Return the latest locally received sample count for software alignment."""

        with self._condition:
            if not self._recording or self._sample_index <= 0:
                return None
            return self._sample_index

    def _open_trigger_transport(self, timeout: float) -> Any:
        transport = str(self.config.get("trigger_transport", "serial")).lower()
        if transport == "tcp":
            host = str(self.config["trigger_host"])
            port = int(self.config.get("trigger_tcp_port", 4321))
            handle = self._trigger_socket_factory((host, port), timeout=timeout)
            return _TcpDcpTransport(handle, timeout)
        if transport != "serial":
            raise ValueError("Neuracle trigger_transport must be serial or tcp")
        return self._open_serial(timeout)

    def _open_serial(self, timeout: float) -> Any:
        port = str(self.config["trigger_port"])
        if self._serial_factory is not None:
            serial_handle = self._serial_factory(port, baudrate=115200, timeout=timeout)
        else:
            try:
                import serial
            except ModuleNotFoundError as error:
                raise RuntimeError("pyserial is required for the Neuracle TriggerBox") from error
            serial_handle = serial.Serial(port, baudrate=115200, timeout=timeout)
        if hasattr(serial_handle, "is_open") and not serial_handle.is_open:
            raise RuntimeError(f"Could not open Neuracle trigger serial port {port}")
        return serial_handle

    def _validate_trigger_device(self) -> None:
        """Perform the validation supported by the configured trigger device.

        TriggerIn has no query command in the vendor API, so opening its serial
        port is its preflight. TriggerBox DCP replies to ``get name`` over either
        serial or TCP transport.
        """

        kind = str(self.config.get("trigger_device", "triggerin")).lower()
        if kind == "triggerin":
            return
        if kind != "triggerbox":
            raise ValueError("Neuracle trigger_device must be triggerin or triggerbox")
        if self._uses_trg_acknowledgement():
            # The acquisition preflight and session self-test still require
            # real samples, hardware_verified TRG and a saved marker. Optional
            # device-info replies must not prevent that end-to-end test.
            return
        timeout = float(self.config.get("connect_timeout_sec", 5))
        try:
            name, _ = self._triggerbox_transaction(4, b"", timeout_sec=timeout)
            info, _ = self._triggerbox_transaction(3, b"\x01", timeout_sec=timeout)
        except RuntimeError as error:
            endpoint = self._trigger_endpoint()
            raise RuntimeError(
                f"Neuracle TriggerBox on {endpoint} did not answer its DCP preflight; "
                "check that the selected serial port or TCP 4321 DCP service is available"
            ) from error
        if not name or len(info) < 8:
            raise RuntimeError("Neuracle TriggerBox returned incomplete device metadata")

    @staticmethod
    def _write_trigger_frame(handle, frame: bytes) -> None:
        written = handle.write(frame)
        if written != len(frame):
            # Never retransmit an ambiguous partial command: that could create
            # an extra physical marker and shift trial identities.
            raise RuntimeError(f'TriggerBox incomplete command write: {written}/{len(frame)} bytes; no retry sent')

    def _triggerbox_transaction(
        self,
        function_id: int,
        payload: bytes,
        *,
        timeout_sec: float,
    ) -> tuple[bytes, int]:
        """Run one DCP command/reply transaction over serial or TCP."""

        assert self._serial is not None
        handle = self._serial
        previous_timeout = getattr(handle, "timeout", None)
        if hasattr(handle, "timeout"):
            handle.timeout = max(0.01, timeout_sec)
        try:
            if hasattr(handle, "reset_input_buffer"):
                handle.reset_input_buffer()
            frame = struct.pack(
                "<BBH", self._TRIGGER_DEVICE_ID, function_id, len(payload)
            ) + payload
            sent_ns = time.monotonic_ns()
            self._write_trigger_frame(handle, frame)
            if hasattr(handle, "flush"):
                handle.flush()
            response = handle.read(4)
            if (
                len(response) != 4
                or response[0] != self._TRIGGER_DEVICE_ID
                or response[1] != function_id
            ):
                detail = response.hex(" ") or "<empty>"
                raise RuntimeError(
                    f"TriggerBox function {function_id} returned invalid header {detail}"
                )
            payload_size = int(response[2]) | (int(response[3]) << 8)
            result = handle.read(payload_size) if payload_size else b""
            if len(result) != payload_size:
                raise RuntimeError(
                    f"TriggerBox function {function_id} returned {len(result)}/"
                    f"{payload_size} payload bytes"
                )
            return result, sent_ns
        finally:
            if hasattr(handle, "timeout"):
                handle.timeout = previous_timeout

    def _read_loop(self) -> None:
        try:
            while not self._stop_reader.is_set() and self._socket is not None:
                try:
                    payload = self._socket.recv(self._recv_size())
                except TimeoutError:
                    with self._condition:
                        last_data = self._last_block_timestamp_ns or self._recording_started_ns
                        stalled = (self._recording and last_data is not None and
                                   time.monotonic_ns() - last_data >
                                   float(self.config.get("stream_stall_timeout_ms", 3000)) * 1_000_000)
                    if stalled:
                        self._mark_disconnected("tcp_stream_stalled: no new EEG samples")
                        return
                    continue
                if not payload:
                    raise ConnectionError("Neuracle TCP stream closed")
                self._process_bytes(payload)
        except Exception as error:  # noqa: BLE001 - reader thread must record any failure as disconnect
            if not self._stop_reader.is_set():
                self._mark_disconnected(str(error))

    def _process_bytes(self, payload: bytes) -> None:
        """Frame and decode JellyFish metadata/data packets from arbitrary TCP chunks."""

        self._rx_buffer += payload
        while True:
            if len(self._rx_buffer) < 2:
                return
            is_meta = self._rx_buffer.startswith(self._META_HEAD)
            is_data = self._rx_buffer.startswith(self._DATA_HEAD)
            if not is_meta and not is_data:
                candidates = [
                    index
                    for index in (
                        self._rx_buffer.find(self._META_HEAD, 1),
                        self._rx_buffer.find(self._DATA_HEAD, 1),
                    )
                    if index >= 0
                ]
                if candidates:
                    self._rx_buffer = self._rx_buffer[min(candidates) :]
                    continue
                self._rx_buffer = self._rx_buffer[-1:]
                return
            if len(self._rx_buffer) < 10:
                return
            total_length = int.from_bytes(self._rx_buffer[6:10], "little", signed=False)
            if total_length < 12 or total_length > self._MAX_PACKET_BYTES:
                raise ValueError(f"Invalid JellyFish packet length: {total_length}")
            if len(self._rx_buffer) < total_length:
                return
            packet, self._rx_buffer = (
                self._rx_buffer[:total_length],
                self._rx_buffer[total_length:],
            )
            expected_tail = self._META_TAIL if is_meta else self._DATA_TAIL
            if not packet.endswith(expected_tail):
                raise ValueError(
                    f"Invalid JellyFish {'metadata' if is_meta else 'data'} packet tail"
                )
            if is_meta:
                self._process_metadata_packet(packet)
            else:
                self._process_data_packet(packet)

    def _process_metadata_packet(self, packet: bytes) -> None:
        if len(packet) < 18:
            raise ValueError("JellyFish metadata packet is too short")
        _, header_length, total_length, flag, module_count = struct.unpack_from("<H4I", packet)
        if header_length != 18 or total_length != len(packet) or module_count < 1:
            raise ValueError("Invalid JellyFish metadata header")
        offsets = struct.unpack_from(f"<{module_count}I", packet, header_length)
        modules: dict[int, dict[str, Any]] = {}
        for index, offset in enumerate(offsets):
            end = offsets[index + 1] if index + 1 < module_count else total_length - 2
            if not header_length <= offset < end <= total_length - 2:
                raise ValueError("Invalid JellyFish metadata module offset")
            module = self._parse_metadata_module(packet[offset:end])
            modules[int(module["serial_number"])] = module
        if flag % 2:
            raise RuntimeError(
                "JellyFish per-module forwarding is not supported here; "
                "select bulk/combined forwarding in JellyFish"
            )
        non_trigger_modules = [serial for serial in modules if serial != 0]
        if len(non_trigger_modules) != 1:
            raise RuntimeError("JellyFish must forward one participant/module in bulk mode")
        serial_number = non_trigger_modules[0]
        module = modules[serial_number]
        forwarded_names = tuple(str(value) for value in module["channel_names"])
        name_to_index: dict[str, int] = {}
        for index, name in enumerate(forwarded_names):
            name_to_index.setdefault(self._channel_key(name), index)
        selected: list[int] = []
        missing: list[str] = []
        for name in self._channel_names():
            index = name_to_index.get(self._channel_key(name))
            if index is None:
                missing.append(name)
            else:
                selected.append(index)
        if missing:
            raise RuntimeError(
                "Configured channels are absent from JellyFish metadata: " + ", ".join(missing)
            )
        sample_rates = tuple(int(value) for value in module["sample_rates"])
        configured_rate = int(self.config["sampling_rate_hz"])
        wrong_rates = {
            sample_rates[index]
            for index in selected
            if sample_rates[index] != configured_rate
        }
        if wrong_rates:
            raise RuntimeError(
                f"JellyFish channel sample rates {sorted(wrong_rates)} do not match "
                f"configured {configured_rate} Hz"
            )
        self._stream_meta = {"flag": flag, "modules": modules}
        self._main_serial_number = serial_number
        self._selected_stream_indices = tuple(selected)
        self._forwarded_channel_count = len(forwarded_names)
        self._module_name = str(module["module_name"])
        self._serial_number = str(serial_number)
        assert self._socket is not None
        acknowledgement = self._META_TAIL + self._META_HEAD
        if hasattr(self._socket, "sendall"):
            self._socket.sendall(acknowledgement)
        else:
            self._socket.send(acknowledgement)
        self._metadata_ready.set()

    @staticmethod
    def _parse_metadata_module(fragment: bytes) -> dict[str, Any]:
        if len(fragment) < 98:
            raise ValueError("JellyFish metadata module is too short")
        person, module, module_type, serial_number, channel_count = struct.unpack_from(
            "<30s30s30s2I", fragment
        )
        expected = 98 + 45 * channel_count
        if len(fragment) != expected:
            raise ValueError(
                f"JellyFish metadata module length is {len(fragment)}, expected {expected}"
            )
        cursor = 98
        channel_names = [
            value.decode("utf-8").rstrip("\x00")
            for value in struct.unpack_from("<" + "10s" * channel_count, fragment, cursor)
        ]
        cursor += 10 * channel_count
        channel_types = [
            value.decode("utf-8").rstrip("\x00")
            for value in struct.unpack_from("<" + "10s" * channel_count, fragment, cursor)
        ]
        cursor += 10 * channel_count
        sample_rates = struct.unpack_from(f"<{channel_count}I", fragment, cursor)
        cursor += 4 * channel_count
        data_counts = struct.unpack_from(f"<{channel_count}I", fragment, cursor)
        return {
            "person_name": person.decode("utf-8").rstrip("\x00"),
            "module_name": module.decode("utf-8").rstrip("\x00"),
            "module_type": module_type.decode("utf-8").rstrip("\x00"),
            "serial_number": serial_number,
            "channel_count": channel_count,
            "channel_names": channel_names,
            "channel_types": channel_types,
            "sample_rates": sample_rates,
            "data_counts": data_counts,
        }

    def _process_data_packet(self, packet: bytes) -> None:
        if self._stream_meta is None or self._main_serial_number is None:
            raise RuntimeError("JellyFish sent data before metadata")
        if len(packet) < 30:
            raise ValueError("JellyFish data packet is too short")
        (
            _,
            header_length,
            total_length,
            _start_timestamp,
            _timestamp_length,
            trigger_count,
            flag,
            module_count,
        ) = struct.unpack_from("<H7I", packet)
        if header_length != 30 or total_length != len(packet) or module_count < 1:
            raise ValueError("Invalid JellyFish data header")
        if flag % 2:
            raise RuntimeError("JellyFish switched away from bulk/combined forwarding")
        offsets = struct.unpack_from(f"<{module_count}I", packet, header_length)
        module_fragments: dict[int, bytes] = {}
        payload_end = total_length - 2 - 34 * trigger_count
        for index, offset in enumerate(offsets):
            end = offsets[index + 1] if index + 1 < module_count else payload_end
            if not header_length <= offset < end <= payload_end:
                raise ValueError("Invalid JellyFish data module offset")
            fragment = packet[offset:end]
            if len(fragment) < 4:
                raise ValueError("JellyFish data module is too short")
            serial_number = int.from_bytes(fragment[:4], "little", signed=False)
            module_fragments[serial_number] = fragment
        try:
            fragment = module_fragments[self._main_serial_number]
            module = self._stream_meta["modules"][self._main_serial_number]
        except KeyError as error:
            raise ValueError("JellyFish data packet omitted the configured module") from error
        channel_count = int(module["channel_count"])
        data_counts = tuple(int(value) for value in module["data_counts"])
        value_count = sum(data_counts)
        values_offset = 4 + channel_count
        expected_length = values_offset + value_count * 4
        if len(fragment) != expected_length:
            raise ValueError(
                f"JellyFish data module length is {len(fragment)}, expected {expected_length}"
            )
        raw = struct.unpack_from(f"<{value_count}f", fragment, values_offset)
        channels: list[tuple[float, ...]] = []
        cursor = 0
        for count in data_counts:
            channels.append(tuple(float(value) for value in raw[cursor : cursor + count]))
            cursor += count
        selected = [channels[index] for index in self._selected_stream_indices]
        sample_count = len(selected[0])
        if sample_count < 1 or any(len(channel) != sample_count for channel in selected):
            raise ValueError("Selected JellyFish channels have unequal data counts")
        received_ns = time.monotonic_ns()
        with self._condition:
            if not self._recording or self._writer is None:
                return
            first_sample_index = self._sample_index
            self._writer.write_block(tuple(selected))
            if self._raw_handle is not None:
                values = [value for sample in zip(*selected, strict=True) for value in sample]
                self._raw_handle.write(struct.pack(f'<{len(values)}f', *values))
                self._raw_handle.flush()
            self._sample_index += sample_count
            for offset, value in enumerate(selected[-1]):
                trigger_code = round(value)
                if trigger_code:
                    # Packet arrival is observable; a per-sample acquisition clock
                    # is not. Backdating by the sample offset can put a real marker
                    # before its command (especially with coarse Windows clocks).
                    # Alignment remains the exact TRG sample, not this timestamp.
                    observed_ns = received_ns
                    self._trigger_events.append(
                        (trigger_code, first_sample_index + offset + 1, observed_ns)
                    )
            self._last_block_timestamp_ns = received_ns
            self._condition.notify_all()

    @staticmethod
    def _channel_key(name: str) -> str:
        normalized = name.strip().upper()
        return "TRG" if normalized in {"TRG", "TRIGGER"} else normalized

    def _mark_disconnected(self, reason: str) -> None:
        with self._condition:
            self._connected = False
            self._error = reason
            self._dropped_blocks += 1
            self._metadata_ready.set()
            if self._recording:
                now = time.monotonic_ns()
                self._gaps.append(
                    DataGap(
                        start_sample_index=self._sample_index + 1,
                        end_sample_index=self._sample_index + 1,
                        missing_samples=0,
                        start_timestamp_ns=now,
                        end_timestamp_ns=now,
                        reason="tcp_disconnected_unknown_gap",
                    )
                )
            self._condition.notify_all()

    def _shutdown_reader(self) -> None:
        self._stop_reader.set()
        self._close_io()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
        self._reader = None
        self._connected = False

    def _close_io(self) -> None:
        for handle in (self._socket, self._serial):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._socket = None
        self._serial = None

    def _close_trigger_transport(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except OSError:
                pass
        self._serial = None

    def _uses_trg_acknowledgement(self) -> bool:
        return str(self.config.get("trigger_acknowledgement", "dcp")).lower() == "trg"

    def _trigger_endpoint(self) -> str:
        if str(self.config.get("trigger_transport", "serial")).lower() == "tcp":
            return (
                f"TCP {self.config.get('trigger_host', '')}:"
                f"{self.config.get('trigger_tcp_port', 4321)}"
            )
        return f"serial {self.config.get('trigger_port', '')}"

    def _recv_size(self) -> int:
        return max(
            4096,
            int(
                0.4
                * max(int(self.config["channel_count"]), self._forwarded_channel_count)
                * int(self.config["sampling_rate_hz"])
                * 4
            ),
        )

    def _channel_names(self) -> tuple[str, ...]:
        names = tuple(str(value) for value in self.config.get("channel_names", ()))
        if names:
            return names
        count = int(self.config["channel_count"])
        return tuple(f"EEG{index:02d}" for index in range(1, count)) + ("TRG",)

    def _validate_config(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ValueError("Neuracle requires devices.neuracle.host")
        transport = str(self.config.get("trigger_transport", "serial")).lower()
        if transport not in {"serial", "tcp"}:
            raise ValueError("Neuracle trigger_transport must be serial or tcp")
        acknowledgement = str(self.config.get("trigger_acknowledgement", "dcp")).lower()
        if acknowledgement not in {"dcp", "trg"}:
            raise ValueError("Neuracle trigger_acknowledgement must be dcp or trg")
        if acknowledgement == "trg" and (transport != "tcp" or str(
                self.config.get("trigger_device", "triggerin")).lower() != "triggerbox"):
            raise ValueError("TRG acknowledgement requires TCP TriggerBox transport")
        if transport == "serial":
            if not str(self.config.get("trigger_port", "")).strip():
                raise ValueError("Neuracle serial trigger requires devices.neuracle.trigger_port")
        else:
            if not str(self.config.get("trigger_host", "")).strip():
                raise ValueError("Neuracle TCP trigger requires devices.neuracle.trigger_host")
            try:
                valid_trigger_port = int(self.config.get("trigger_tcp_port", 4321)) > 0
            except (TypeError, ValueError):
                valid_trigger_port = False
            if not valid_trigger_port:
                raise ValueError(
                    "Neuracle TCP trigger requires a positive devices.neuracle.trigger_tcp_port"
                )
        for key in ("port", "sampling_rate_hz", "channel_count"):
            try:
                valid = int(self.config.get(key, 0)) > 0
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError(f"Neuracle requires a positive devices.neuracle.{key}")
        for key in ("trigger_command_timeout_ms", "trigger_verification_timeout_ms"):
            if key not in self.config:
                continue
            try:
                valid = float(self.config[key]) > 0
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError(f"Neuracle requires a positive devices.neuracle.{key}")
        channel_count = int(self.config["channel_count"])
        if channel_count < 2:
            raise ValueError("Neuracle channel_count must include EEG and TRG channels")
        names = self._channel_names()
        if len(names) != channel_count or names[-1].upper() != "TRG":
            raise ValueError("Neuracle channel_names must match channel_count and end with TRG")
        units = tuple(self.config.get("channel_units", ()))
        if units and len(units) != channel_count:
            raise ValueError("Neuracle channel_units must match channel_count")
        if str(self.config.get("trigger_device", "triggerin")).lower() not in {
            "triggerin",
            "triggerbox",
        }:
            raise ValueError("Neuracle trigger_device must be triggerin or triggerbox")
        if transport == "tcp" and str(
            self.config.get("trigger_device", "triggerin")
        ).lower() != "triggerbox":
            raise ValueError("Neuracle TCP DCP transport requires trigger_device=triggerbox")


def create_device(config: dict[str, Any]) -> NeuracleDevice:
    """Adapter factory for ``devices.neuracle.adapter_factory``."""

    return NeuracleDevice(
        NeuracleTcpApi(config),
        allow_software_trigger_fallback=bool(
            config.get("allow_software_trigger_fallback", False)
        ),
    )
