"""BC-BG1B firmware 2.0.5 framing compatibility for SDK 0.5.0.

The device sends ten samples in protobuf slots 1..4 and 8..13; SDK 0.5.0
only buffers slots 1..4. Preserve the original stream for SDK control and
decode ALL sample slots using the SDK's own ADC conversion function.
See logs/tcp-capture/wire-analysis.json for the captured regression evidence.
"""
import asyncio
import threading
import time
import json
from pathlib import Path
from brainco_sequence import SampleOrder, SequenceError


class WireError(RuntimeError):
    pass


def _varint(data, pos):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise WireError('Truncated protobuf varint')
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise WireError('Oversized protobuf varint')


def fields(data):
    result = {}
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number or number in result:
            raise WireError('Unexpected/duplicate protobuf field')
        if wire == 0:
            value, pos = _varint(data, pos)
        else:
            if wire == 2:
                size, pos = _varint(data, pos)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                raise WireError('Unsupported protobuf wire type')
            if pos + size > len(data):
                raise WireError('Truncated protobuf field')
            value = bytes(data[pos:pos+size])
            pos += size
        result[number] = (wire, value)
    return result


def _crc_entry(value):
    for _ in range(8):
        value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value


_CRC_TABLE = tuple(_crc_entry(i) for i in range(256))


def crc16(data):
    value = 0xFFFF
    for byte in data:
        value = (value >> 8) ^ _CRC_TABLE[(value ^ byte) & 255]
    return value


class FrameDecoder:
    def __init__(self, convert, rate, gain, await_normal_start=False, reorder_samples=0,
                 require_start_command=False, startup_guard_seconds=0):
        self.convert = convert
        self.rate_enum = {250: 1, 500: 2, 1000: 3, 2000: 4}[int(rate)]
        self.gain_enum = {1: 1, 2: 2, 4: 3, 6: 4, 8: 5, 12: 6, 24: 7}[gain]
        self.pending = bytearray()
        self.frames = 0
        self.eeg_frames = 0
        self.samples = 0
        self.missing = 0
        self.last_index = None
        self.await_normal_start = await_normal_start
        self.rate = int(rate)
        self.order = SampleOrder(reorder_samples) if reorder_samples else None
        self.anchor = None
        self.latest_anchor = None
        self.await_start_command = require_start_command
        self.startup_guard_seconds = float(startup_guard_seconds)
        if self.startup_guard_seconds and self.order is None:
            raise ValueError('Startup quarantine requires the sample reorder buffer')
        self.startup_done = not self.startup_guard_seconds
        self.startup_since = None
        self.last_received = None
        self.startup_discarded = 0
        self.startup_resets = []

    def arm_start_command(self):
        if self.samples:
            raise WireError('Cannot restart an active EEG timeline; open a new recording')
        self.await_start_command = False
        self.startup_since = None
        self.anchor = self.latest_anchor = None
        if self.order is not None:self.order = SampleOrder(self.order.window)

    def feed(self, data, received=None):
        received = time.perf_counter() if received is None else received
        anchor_this_receive = self.anchor is None
        self.pending.extend(data)
        rows = []
        while len(self.pending) >= 11:
            if self.pending[:6] != b'BRNC\x02\x0b' or self.pending[8:11] != b'\x00\x02\x00':
                raise WireError('Unexpected BrainCo frame header; refusing guessed decoding')
            size = int.from_bytes(self.pending[6:8], 'little') + 13
            if size > 8192:
                raise WireError('Oversized BrainCo frame')
            if len(self.pending) < size:
                break
            frame = bytes(self.pending[:size])
            del self.pending[:size]
            if crc16(frame[:-2]) != int.from_bytes(frame[-2:], 'little'):
                raise WireError('BrainCo frame CRC mismatch')
            self.frames += 1
            envelope = fields(frame[11:-2])
            if 3 not in envelope:
                continue  # IMU/device info, handled independently by the SDK.
            wire, eeg = envelope[3]
            if wire != 2:
                raise WireError('Invalid EEG envelope')
            eeg = fields(eeg)
            if self.await_normal_start:
                # A reconnect may receive buffered data from the previous mode.
                # Only the device's NORMAL acknowledgement opens a new stream;
                # never treat old impedance/session samples as current EEG.
                if eeg.get(4) == (0, 1):
                    self.await_normal_start = False
                continue
            if 2 not in eeg:
                continue  # Configuration/mode acknowledgement.
            if self.await_start_command:
                continue  # NORMAL configuration is not start_eeg_stream.
            wire, payload = eeg[2]
            if wire != 2:
                raise WireError('Invalid EEG payload')
            payload = fields(payload)
            if payload.get(6) != (0, self.rate_enum) or payload.get(7) != (0, self.gain_enum):
                raise WireError('Device EEG sample rate/gain does not match requested configuration')
            slots = [1, 2, 3, 4, 8, 9, 10, 11, 12, 13]
            if set(payload) - set(slots + [5, 6, 7]):
                raise WireError('Unknown EEG payload layout')
            if 5 in payload:
                raise WireError('Unverified EEG payload field 5')
            present = [slot for slot in slots if slot in payload]
            if present not in (slots[:4], slots):
                raise WireError('Unsupported EEG sample-slot layout')
            self.eeg_frames += 1
            frame_rows = []
            for slot in present:
                wire, sample = payload[slot]
                if wire != 2:
                    raise WireError('Invalid EEG sample')
                sample = fields(sample)
                index = sample.get(1, (0, 0))  # proto3 omits a zero scalar.
                adc = sample.get(2)
                if set(sample) - {1, 2} or index[0] != 0 or adc is None or adc[0] != 2 or len(adc[1]) != 108:
                    raise WireError('Invalid EEG counter/ADC layout')
                index = index[1]
                if self.order is None and self.last_index is not None:
                    delta = index - self.last_index
                    if delta <= 0:
                        raise WireError(f'EEG counter reset/wrap/duplicate; start a new recording; previous={self.last_index}, current={index}')
                    self.missing += delta - 1
                values = self.convert(adc[1])
                if len(values) != 32:
                    raise WireError('ADC decoder did not return 32 EEG channels')
                frame_rows.append((index, values, adc[1]))
                if self.order is None:
                    rows.append((index, values))
                    self.last_index = index
                    self.samples += 1
            self.last_received = received
            if not self.startup_done:
                if self.startup_since is None:self.startup_since = received
                # Firmware can combine the previous run's tail and the new
                # run's counters in ONE protobuf frame (observed 1462..1465,
                # 2..7). Inspect before inserting any slot into SampleOrder.
                drops = [i for i in range(1, len(frame_rows))
                         if frame_rows[i-1][0]-frame_rows[i][0]>self.order.window]
                if drops:
                    split = drops[0]
                    segments = (frame_rows[:split], frame_rows[split:])
                    contiguous = all(all(b[0] == a[0]+1 for a, b in zip(part, part[1:]))
                                     for part in segments)
                    low = frame_rows[split][0]
                    if (len(drops)!=1 or not contiguous or low>self.rate
                            or self.startup_resets or self.order.emitted):
                        raise WireError('Unstable EEG startup: repeated or unverified mixed-frame reset')
                    self.startup_resets.append(dict(
                        previous_highest=max(row[0] for row in frame_rows[:split]),
                        new_counter=low, received=received, reason='mixed_frame',
                        discarded_frame_counters=[row[0] for row in frame_rows]))
                    self.startup_discarded += len(self.order.pending)+len(frame_rows)
                    self.order = SampleOrder(self.order.window)
                    self.startup_since = None
                    self.anchor = self.latest_anchor = None
                    anchor_this_receive = True
                    # Preserve the full mixed frame in rx.bin; none of its
                    # ambiguous samples enters the formal recording/clock.
                    continue
                low=min(row[0] for row in frame_rows)
                high=self.order.highest
                if high is not None and high-low>self.order.window:
                    # Only one near-zero startup reset, before any rows leave
                    # quarantine. Retain all bytes in rx.bin for later audit.
                    if low>self.rate or self.startup_resets or self.order.emitted:
                        raise WireError('Unstable EEG startup: repeated or unverified counter reset')
                    self.startup_resets.append(dict(previous_highest=high,new_counter=low,received=received))
                    self.startup_discarded += len(self.order.pending)
                    self.order=SampleOrder(self.order.window)
                    self.startup_since=received
                    self.anchor=self.latest_anchor=None
                    anchor_this_receive=True
            latest = max(r[0] for r in frame_rows)
            if self.latest_anchor is None or latest > self.latest_anchor['index']:
                self.latest_anchor = dict(index=latest, received=received)
            if anchor_this_receive:
                # Timestamp of the first observed frame's newest counter, BEFORE buffering.
                # This preserves the pre-existing receive-anchor assumption and does not
                # move EEG forward by the reorder buffer's delay.
                latest = max(r[0] for r in frame_rows)
                self.anchor = dict(index=max(latest, self.anchor['index']) if self.anchor else latest,
                                   received=received)
            if self.order is not None:
                try:
                    for index, values, raw in frame_rows:
                        self.order.add(index, values, raw)
                    rows.extend(self.flush_order())
                except SequenceError as exc:
                    raise WireError(str(exc)) from exc
        return rows

    def flush_order(self, final=False):
        if self.order is None:
            return []
        if not self.startup_done:
            if self.startup_since is None or self.last_received-self.startup_since<self.startup_guard_seconds:
                return []
            self.startup_done=True
        rows = self.order.take(flush=final)
        self.samples = self.order.emitted
        self.missing = self.order.missing
        self.last_index = self.order.last
        return rows

    def diagnostics(self):
        return dict(frames=self.frames, eeg_frames=self.eeg_frames, samples=self.samples,
                    missing=self.missing, anchor=self.anchor,
                    awaiting_start_command=self.await_start_command,
                    startup_ready=self.startup_done, startup_discarded_samples=self.startup_discarded,
                    startup_resets=self.startup_resets,
                    sequence=self.order.diagnostics() if self.order is not None else None)


class WireTap:
    """Loopback-only transparent relay; socket read boundaries are not frames."""
    def __init__(self, decoder, limit, archive=None):
        self.decoder = decoder
        self.limit = limit
        self.lock = threading.Lock()
        self.rows = []
        self.error = None
        self.server = None
        self.tasks = set()
        self.writers = set()
        self.closing = False
        self.connected = False
        self.upstream_ready = False
        self.archive = Path(archive) if archive else None
        self.raw_file = None
        self.chunk_file = None
        self.raw_bytes = 0
        if self.archive is not None:
            self.archive.mkdir(parents=True, exist_ok=False)
            self.raw_file = (self.archive/'rx.bin').open('xb')
            self.chunk_file = (self.archive/'chunks.jsonl').open('x', encoding='utf-8')

    async def start(self, host, port):
        self.host, self.port = host, port
        if self.archive is not None:
            (self.archive/'metadata.json').write_text(json.dumps(dict(host=host, port=port,
                sample_rate=self.decoder.rate, gain_enum=self.decoder.gain_enum,
                started_monotonic=time.perf_counter(), started_unix=time.time(),
                awaiting_normal_start=self.decoder.await_normal_start,
                reorder_samples=self.decoder.order.window if self.decoder.order else 0), indent=2), encoding='utf-8')
        self.server = await asyncio.start_server(self._session, '127.0.0.1', 0)
        return self.server.sockets[0].getsockname()[1]

    async def wait_connected(self, timeout=5):
        """The SDK's loopback connection alone does not prove device connectivity."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            with self.lock:
                error, ready = self.error, self.upstream_ready
            if error:
                raise WireError(error)
            if ready:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise WireError(f'脑电帽网络连接超时：{self.host}:{self.port}，尚未开始 EEG 采集。请检查设备电源、网络及 IP 地址。')
            await asyncio.sleep(.01)

    async def _relay(self, reader, writer, incoming):
        while data := await reader.read(65536):
            if incoming:
                received = time.perf_counter()
                if self.raw_file is not None:
                    self.raw_file.write(data)
                    self.raw_file.flush()
                    self.chunk_file.write(json.dumps(dict(offset=self.raw_bytes, size=len(data), received=received))+'\n')
                    self.chunk_file.flush()
                    self.raw_bytes += len(data)
                with self.lock:
                    rows = self.decoder.feed(data, received=received)
                    if len(self.rows) + len(rows) > self.limit:
                        raise WireError('EEG reader queue overflow; acquisition cannot keep up')
                    self.rows.extend(rows)
                    self.received = time.perf_counter()
            writer.write(data)
            await writer.drain()

    async def _session(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        parts = []
        remote_writer = None
        try:
            if self.connected:
                raise WireError('Only one SDK connection is allowed')
            self.connected = True
            try:
                remote_reader, remote_writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=5)
            except (OSError, asyncio.TimeoutError) as exc:
                raise WireError(f'脑电帽网络连接失败：{self.host}:{self.port}（{str(exc) or "连接超时"}），尚未开始 EEG 采集。请检查设备电源、网络及 IP 地址。') from exc
            with self.lock:
                self.upstream_ready = True
            self.writers.update((writer, remote_writer))
            parts = [asyncio.create_task(self._relay(reader, remote_writer, False)),
                     asyncio.create_task(self._relay(remote_reader, writer, True))]
            finished, _ = await asyncio.wait(parts, return_when=asyncio.FIRST_COMPLETED)
            for part in finished:
                part.result()
            if not self.closing:
                raise WireError('BrainCo TCP stream disconnected')
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
        finally:
            for part in parts:
                part.cancel()
            await asyncio.gather(*parts, return_exceptions=True)
            for item in (writer, remote_writer):
                if item is not None:
                    item.close()
                    self.writers.discard(item)
            self.tasks.discard(task)

    def take(self, flush=False):
        with self.lock:
            if self.error:
                raise WireError(self.error)
            if flush:
                self.rows.extend(self.decoder.flush_order(final=True))
            result, self.rows = self.rows, []
            return result

    async def close(self):
        self.closing = True
        if self.server:
            self.server.close()
        for writer in list(self.writers):
            writer.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Python 3.12 waits for accepted transports too: close sessions first.
        if self.server:
            await self.server.wait_closed()
        if self.raw_file is not None:
            self.raw_file.close()
            self.chunk_file.close()
            self.raw_file = None
            self.chunk_file = None
            (self.archive/'summary.json').write_text(json.dumps(dict(
                error=self.error, raw_bytes=self.raw_bytes, **self.decoder.diagnostics()), indent=2), encoding='utf-8')
