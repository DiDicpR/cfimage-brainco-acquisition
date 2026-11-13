"""EEG source boundary and auditable, bounded in-memory capture."""
import threading
import time
from collections import deque
import numpy as np

# Acquisition, replay, and child workers share perf_counter's system-wide clock.
# Python 3.12 on Windows uses a coarse 15.6 ms clock for time.monotonic().

class EmptyCaptureWindow(RuntimeError):
    """Stopped before any sample fell inside the replay window; not a disk failure."""


class SimulatedSource:
    def describe(self):
        return dict(real=False, sample_rate=250, channels=['SIM1', 'SIM2'], units='synthetic',
                    host_clock='time.perf_counter',
                    clock_description='host monotonic; synthetic 10/12 Hz; NOT BrainCo')

    def start(self, on_sample, on_error):
        self.done = threading.Event()
        def loop():
            try:
                base = time.perf_counter()
                n = 0
                while not self.done.is_set():
                    t = base + n / 250
                    stopped = self.done.wait(max(0, t - time.perf_counter()))
                    # Windows waits may wake late. Emit all due synthetic samples,
                    # including the final batch when stop wakes the thread.
                    until = time.perf_counter()
                    while base + n / 250 <= until:
                        on_sample(base + n / 250, [float(np.sin(n * 2 * np.pi * 10 / 250)),
                                                  float(np.cos(n * 2 * np.pi * 12 / 250))], n)
                        n += 1
                    if stopped:
                        break
            except Exception as exc:
                on_error(str(exc))
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def stop(self):
        if hasattr(self, 'done'):
            self.done.set()
            self.thread.join(2)


def make_source(mode):
    if mode == 'simulated':
        return SimulatedSource()
    if mode == 'brainco':
        from brainco_adapter import BrainCoSource
        return BrainCoSource()
    raise ValueError('未知 EEG 来源')


class Capture:
    def __init__(self, source, archive=None, segment=None, buffer_seconds=None):
        self.source = source
        self.meta = source.describe()
        if not self.meta.get('channels') or not 0 < self.meta['sample_rate'] <= 10000:
            raise ValueError('EEG 通道/采样率元数据无效')
        self.rows = deque() if buffer_seconds is not None else []
        self.archive = archive
        self.segment = segment
        self.buffer_seconds = buffer_seconds
        self.pinned_start = None
        self.lock = threading.Lock()
        self.error = None
        self.last_arrival = None

    def on_error(self, message):
        with self.lock:
            if not self.error:
                self.error = str(message)
                if self.archive:
                    try:self.archive.event('source_error',segment=self.segment,message=str(message))
                    except Exception:pass

    def pin(self, start=None):
        """Retain the current replay until its legacy slice/context have been saved."""
        with self.lock:self.pinned_start=start

    def on_sample(self, t, values, device_timestamp=None):
        with self.lock:
            now = time.perf_counter()
            v = np.asarray(values, dtype=np.float64)
            if self.archive and v.shape == (len(self.meta['channels']),) and np.isfinite(v).all() and np.isfinite(t):
                try:self.archive.sample(self.segment,t,v.tolist(),device_timestamp,now)
                except Exception as exc:self.error=str(exc)
            if self.error:
                return
            if v.shape != (len(self.meta['channels']),) or not np.isfinite(v).all():
                self.error = 'EEG 通道数变化或含非有限值'
            elif not np.isfinite(t) or (not self.meta.get('nominal_timestamps') and abs(t - now) > 10):
                self.error = 'EEG 时间戳不在本进程 monotonic 时钟域或滞后超过 10 秒'
            elif self.rows and t <= self.rows[-1][0]:
                self.error = 'EEG 时间戳重复/倒序'
            elif self.buffer_seconds is None and len(self.rows) >= self.meta['sample_rate'] * 1800:
                self.error = '单次采集超过 30 分钟容量'
            else:
                self.rows.append((float(t), v.tolist(), device_timestamp, now))
                self.last_arrival = now
                if self.buffer_seconds is not None:
                    cutoff=t-self.buffer_seconds
                    if self.pinned_start is not None:cutoff=min(cutoff,self.pinned_start)
                    while self.rows and self.rows[0][0]<cutoff:self.rows.popleft()

    def start(self):
        self.source.start(self.on_sample, self.on_error)

    def stop(self):
        self.source.stop()

    def check(self):
        if self.archive:self.archive.check()
        with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            if self.last_arrival is not None and time.perf_counter() - self.last_arrival > 2:
                raise RuntimeError('脑电超过 2 秒没有新数据；本轮不能标记为完整采集')

    def ready(self, stop, timeout=5):
        timeout = getattr(self.source, 'ready_timeout', timeout)
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.check()
            with self.lock:
                if len(self.rows) >= 2:
                    return
            if stop.wait(.02):
                raise RuntimeError('已取消')
        raise RuntimeError('EEG 连接后未收到有效样本')

    def wait_for_span(self, seconds, stop, timeout=3):
        """Collect actual sample history before replay; no synthetic padding."""
        if seconds <= 0:
            return
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.check()
            if stop.is_set():
                raise RuntimeError('已取消')
            with self.lock:
                if self.rows and self.rows[-1][0] - self.rows[0][0] >= seconds:
                    return
            stop.wait(.01)
        raise RuntimeError('EEG 预缓冲未收到足够的连续时间范围，请检查采集连接')

    def wait_until_sample(self, target, stop, timeout=2):
        """Wait for sample-time coverage, not merely a fixed wall-clock sleep."""
        now = time.perf_counter()
        with self.lock:
            latest = self.rows[-1][0] if self.rows else now
        # Continuous connections can have a growing host/sample clock offset.
        # The time still needed is measured from the latest sample, NOT from
        # the current host clock. Do not change timestamps or fill any gaps.
        deadline = now + max(0., target-latest) + timeout
        self.last_coverage_wait = dict(target=target,started=now,last_sample_at_start=latest,
            sample_clock_lag=max(0.,now-latest),budget_seconds=deadline-now)
        while not stop.is_set():
            self.check()
            with self.lock:
                if self.rows and self.rows[-1][0] >= target:
                    self.last_coverage_wait.update(covered=True,elapsed_seconds=time.perf_counter()-now)
                    return True
            if time.perf_counter() >= deadline:
                self.last_coverage_wait.update(covered=False,elapsed_seconds=time.perf_counter()-now)
                return False
            stop.wait(.01)
        self.last_coverage_wait.update(covered=False,cancelled=True,elapsed_seconds=time.perf_counter()-now)
        return False

    def window(self, start, end):
        with self.lock:
            rows = [r for r in self.rows if start <= r[0] < end]
        if not rows:
            raise EmptyCaptureWindow('回放窗口内无 EEG 样本')
        t = np.array([r[0] for r in rows])
        x = np.array([r[1] for r in rows])
        arrival = np.array([r[3] for r in rows])
        rate = self.meta['sample_rate']
        gaps = np.diff(t)
        report = dict(samples=len(rows), expected_samples=(end-start)*rate,
                      duration=end-start, max_sample_gap=float(gaps.max()) if len(gaps) else 0,
                      first_sample_delay=float(t[0]-start), last_sample_gap=float(end-t[-1]),
                      gap_count=int(np.sum(gaps > 1.8/rate)),
                      sample_coverage=len(rows)/max(1, (end-start)*rate),
                      max_arrival_lag=float(np.max(arrival-t)), acquisition_error=self.error)
        report['timestamp_quality'] = self.meta.get('timestamp_quality', 'synthetic')
        report['hardware_sync_verified'] = False
        report['packet_loss_detection'] = self.meta.get('packet_loss_detection', False)
        report['sample_loss_detection'] = self.meta.get('sample_loss_detection', False)
        report['complete'] = bool(not self.error and report['gap_count'] == 0
                                  and report['first_sample_delay'] < 2.5/rate
                                  and report['last_sample_gap'] < 2.5/rate
                                  and .98 <= report['sample_coverage'] <= 1.02)
        return t, x, arrival, [r[2] for r in rows], report


def align_labels(sample_times, markers, intervals):
    """Map dataset-time annotations to measured command-start times, no fixed duration assumption."""
    if len(markers) < 2:
        raise ValueError('缺少回放时钟锚点')
    src = np.array([m['source_seconds'] for m in markers])
    host = np.array([m['monotonic'] for m in markers])
    if (np.diff(src) <= 0).any() or (np.diff(host) <= 0).any():
        raise ValueError('回放锚点必须严格递增')
    mask = np.zeros((len(sample_times), len(intervals)), dtype=np.uint8)
    mapped = []
    for i, event in enumerate(intervals):
        if event['start'] >= src[-1]:
            continue
        a, b = np.interp([event['start'], min(event['end'], src[-1])], src, host)
        mask[:, i] = (sample_times >= a) & (sample_times < b)
        mapped.append(dict(**event, start_monotonic=float(a), end_monotonic=float(b),
                           label_column=i, truncated=bool(event['end'] > src[-1])))
    return mask, mapped
