from __future__ import annotations

import math
import struct
from datetime import datetime
from pathlib import Path
from typing import Self


def _field(value: object, width: int) -> bytes:
    encoded = str(value).encode("ascii", "replace")[:width]
    return encoded.ljust(width, b" ")


class EdfWriter:
    """Small EDF writer for continuous integer EEG blocks.

    Data are written in one-second records. The final partial record is padded
    with its last value and the exact acquired sample count stays in metadata.
    """

    def __init__(
        self,
        path: Path,
        *,
        channel_names: tuple[str, ...],
        sampling_rate_hz: int,
        units: tuple[str, ...] | None = None,
    ) -> None:
        if not channel_names or sampling_rate_hz < 1:
            raise ValueError("EDF requires channels and a positive integer sampling rate")
        self.path = path
        self.channel_names = channel_names
        self.sampling_rate_hz = sampling_rate_hz
        self.units = units or tuple("uV" for _ in channel_names)
        if len(self.units) != len(channel_names):
            raise ValueError("EDF units must match channel count")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w+b")
        self._buffers = [[] for _ in channel_names]
        self.sample_count = 0
        self.record_count = 0
        self._start_time = datetime.now()
        self._write_header(-1)

    def _write_header(self, record_count: int) -> None:
        channel_count = len(self.channel_names)
        header_bytes = 256 + channel_count * 256
        fixed = b"".join(
            (
                _field("0", 8),
                _field("X", 80),
                _field("THINGS-EEG2-Extended", 80),
                _field(self._start_time.strftime('%d.%m.%y'), 8),
                _field(self._start_time.strftime('%H.%M.%S'), 8),
                _field(header_bytes, 8),
                _field("", 44),
                _field(record_count, 8),
                _field("1", 8),
                _field(channel_count, 4),
            )
        )
        per_signal = (
            [_field(name, 16) for name in self.channel_names]
            + [_field("", 80) for _ in self.channel_names]
            + [_field(unit, 8) for unit in self.units]
            + [_field("-32768", 8) for _ in self.channel_names]
            + [_field("32767", 8) for _ in self.channel_names]
            + [_field("-32768", 8) for _ in self.channel_names]
            + [_field("32767", 8) for _ in self.channel_names]
            + [_field("", 80) for _ in self.channel_names]
            + [_field(self.sampling_rate_hz, 8) for _ in self.channel_names]
            + [_field("", 32) for _ in self.channel_names]
        )
        self._handle.seek(0)
        self._handle.write(fixed + b"".join(per_signal))
        self._handle.seek(0, 2)

    def write_block(self, channels: tuple[tuple[float, ...], ...]) -> None:
        if len(channels) != len(self.channel_names):
            raise ValueError("EDF block channel count mismatch")
        lengths = {len(values) for values in channels}
        if len(lengths) != 1:
            raise ValueError("EDF block channels must have equal sample counts")
        block_samples = lengths.pop()
        converted: list[list[int]] = []
        for values in channels:
            channel: list[int] = []
            for value in values:
                if not math.isfinite(value):
                    raise ValueError("EDF samples must be finite")
                channel.append(max(-32768, min(32767, round(value))))
            converted.append(channel)
        self.sample_count += block_samples
        for buffer, values in zip(self._buffers, converted, strict=True):
            buffer.extend(values)
        while len(self._buffers[0]) >= self.sampling_rate_hz:
            self._write_record(self.sampling_rate_hz)

    def _write_record(self, count: int) -> None:
        for channel_name, buffer in zip(self.channel_names, self._buffers, strict=True):
            values = buffer[:count]
            del buffer[:count]
            if len(values) < self.sampling_rate_hz:
                values.extend(
                    [0 if channel_name.upper() == 'TRG' else (values[-1] if values else 0)]
                    * (self.sampling_rate_hz - len(values))
                )
            self._handle.write(struct.pack(f"<{self.sampling_rate_hz}h", *values))
        self.record_count += 1
        self._handle.flush()

    def close(self) -> None:
        if self._handle.closed:
            return
        remaining = len(self._buffers[0])
        if remaining:
            self._write_record(remaining)
        self._write_header(self.record_count)
        self._handle.flush()
        self._handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def sine_block(
    *, first_sample: int, sample_count: int, channel_count: int, sampling_rate_hz: int
) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(
            100.0
            * math.sin(
                2.0 * math.pi * (8.0 + channel) * (first_sample + index) / sampling_rate_hz
            )
            for index in range(sample_count)
        )
        for channel in range(channel_count)
    )
