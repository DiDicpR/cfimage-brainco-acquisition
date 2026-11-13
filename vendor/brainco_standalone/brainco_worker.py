"""Internal acquisition worker. Not an independent experiment/robot launcher."""
import json
import logging
import sys
import threading
import time
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.deps'))
sys.path.insert(0, str(ROOT / 'vendor'))


class EstimatedClock:
    """Continuous sample count, first-batch receive anchor. Unknown absolute latency."""
    def __init__(self, rate):
        self.rate = rate
        self.origin = None
        self.count = 0

    def batch(self, size, received):
        if size <= 0:
            raise ValueError('Empty batch')
        if self.origin is None:
            self.origin = received-(size-1)/self.rate
        start = self.origin+self.count/self.rate
        result = dict(estimated_start=start, receive_monotonic=received,
                      sample_index_start=self.count, samples=size,
                      estimated_last_sample_lag=received-(start+(size-1)/self.rate))
        self.count += size
        return result


class CounterClock:
    """Preserve device sequence gaps; host anchor still has unknown latency."""
    def __init__(self, rate):
        self.rate = rate
        self.origin = None
        self.first_index = None
        self.last_index = None
        self.count = 0
        self.missing = 0

    def batch(self, indices, received, anchor=None):
        if not indices or any(b <= a for a, b in zip(indices, indices[1:])):
            raise ValueError('Invalid device sample sequence')
        if self.last_index is not None and indices[0] <= self.last_index:
            raise ValueError('Device sample sequence reset or duplicated')
        if self.origin is None:
            self.first_index = indices[0]
            self.origin = (anchor['received']+(indices[0]-anchor['index'])/self.rate
                           if anchor is not None else received-(indices[-1]-indices[0])/self.rate)
        previous = indices[0]-1 if self.last_index is None else self.last_index
        self.missing += indices[-1]-previous-len(indices)
        self.last_index = indices[-1]
        start = self.origin+(indices[0]-self.first_index)/self.rate
        self.count += len(indices)
        return dict(estimated_start=start, receive_monotonic=received,
                    sample_index_start=indices[0]-self.first_index, samples=len(indices),
                    sample_offsets=[i-indices[0] for i in indices], device_sample_indices=indices,
                    missing_samples_total=self.missing,
                    estimated_last_sample_lag=received-(self.origin+(indices[-1]-self.first_index)/self.rate))


def main():
    import os
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    def send(row):
        protocol.write(json.dumps(row, allow_nan=False)+'\n')
        protocol.flush()
    from brainco_adapter import validate_config
    from brainco_full_stream import FullStreamAcquirer
    config = validate_config(json.loads(sys.stdin.readline()))
    clock = CounterClock(config['sample_rate'])
    done = threading.Event()
    def commands():
        for line in sys.stdin:
            if json.loads(line).get('stop'):
                done.set()
                return
        done.set()
    threading.Thread(target=commands, daemon=True).start()

    class InstrumentedAcquirer(FullStreamAcquirer):
        def _drain_eeg_buffer(self):
            started = time.perf_counter()
            data = super()._drain_eeg_buffer()
            ended = time.perf_counter()
            if ended-getattr(self, 'last_poll_report', 0) >= .5:
                self.last_poll_report = ended
                send(dict(type='poll_diagnostics', receive_monotonic=ended,
                          call_seconds=ended-started, raw_shape=getattr(self, 'last_raw_shape', None),
                          normalized_shape=list(data.shape), raw_packet_count=self._raw_packet_count,
                          total_samples=self._total_samples_seen))
            return data

        def _resolve_addr_port(self):
            from brainco_adapter import resolve_target
            return resolve_target(config, self._discover_device_via_zeroconf_blocking)

        def _append_eeg_samples(self, data, *, from_callback):
            super()._append_eeg_samples(data, from_callback=from_callback)
            if data.shape[0] != 32 or not np.isfinite(data).all():
                raise RuntimeError('BrainCo 数据不是有效的 32 通道样本')
            if data.shape[1]:
                received = time.perf_counter()
                send(dict(type='batch', **clock.batch(self.last_sample_indices, received,
                          anchor=self.wire.decoder.anchor), packet_anchor=self.wire.decoder.latest_anchor, values=data.T.tolist()))

    acquirer = InstrumentedAcquirer(sfreq=config['sample_rate'], n_channels=32, buffer_sec=5,
        brainco_addr=config['address'], brainco_port=config['port'], auto_discover=config['auto_discover'],
        scan_timeout_sec=config['scan_timeout'], ready_timeout_sec=config['ready_timeout'],
        start_retries=1, eeg_gain=config['gain'], signal_source='NORMAL', device_id=config['device_id'])
    try:
        acquirer.start_stream()
        send(dict(type='connected', host=acquirer._wire_target[0],
                  port=acquirer._wire_target[1], monotonic=time.perf_counter(),
                  decoder='bc-bg1b-ten-slot-reorder-v2', channel_layout='device_counter_then_32_eeg'))
        while not done.wait(.01):
            acquirer.get_new_samples()
        drain_until = time.perf_counter()+.3
        while time.perf_counter()<drain_until:
            acquirer.get_new_samples()
            time.sleep(.01)
        acquirer.flush_samples()
        send(dict(type='stopping', sample_count=clock.count))
    except Exception as exc:
        send(dict(type='error', message=str(exc)))
    finally:
        try:
            acquirer.stop_stream()
        finally:
            if getattr(acquirer, 'wire', None) is not None:
                send(dict(type='wire_diagnostics', **acquirer.wire.decoder.diagnostics(),
                          archive=os.environ.get('BRAINCO_WIRE_ARCHIVE')))
            send(dict(type='stopped', monotonic=time.perf_counter(), sample_count=clock.count))


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    main()
