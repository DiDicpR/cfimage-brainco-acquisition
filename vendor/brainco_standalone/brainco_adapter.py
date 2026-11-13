"""BrainCo SDK bridge; native SDK runs in a disposable acquisition subprocess."""
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
from collections import deque

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / '.deps'))
CONFIG = ROOT / 'brainco_settings.json'
DEFAULTS = dict(address='', port=0, auto_discover=True, sample_rate=250, channels=32,
                gain=6, scan_timeout=6, ready_timeout=20, device_id='eeg-cap')


def validate_config(value):
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise ValueError('未知 BrainCo 配置项')
    d = dict(DEFAULTS, **value)
    for key in ('port', 'sample_rate', 'channels', 'gain', 'scan_timeout', 'ready_timeout'):
        if isinstance(d[key], bool) or not isinstance(d[key], int):
            raise ValueError(f'{key} 必须为整数')
    if d['sample_rate'] not in (250, 500, 1000, 2000) or d['channels'] != 32:
        raise ValueError('当前按 demo 的 32 通道采集；采样率为 250/500/1000/2000')
    if d['gain'] not in (1, 2, 4, 6, 8, 12, 24) or not 0 <= d['port'] <= 65535:
        raise ValueError('增益或 TCP 端口无效')
    if not 1 <= d['scan_timeout'] <= 10 or not 1 <= d['ready_timeout'] <= 20:
        raise ValueError('发现超时 1–10 秒，SDK 响应超时 1–20 秒')
    if not isinstance(d['auto_discover'], bool):
        raise ValueError('auto_discover 必须为布尔值')
    for key in ('address', 'device_id'):
        if not isinstance(d[key], str) or len(d[key]) > 200 or any(ord(c)<32 for c in d[key]):
            raise ValueError(f'{key} 无效')
        d[key] = d[key].strip()
    if not d['device_id']:
        raise ValueError('device_id 不能为空')
    if not d['auto_discover'] and (not d['address'] or not d['port']):
        raise ValueError('手动连接需填写脑电帽 IP 和 TCP 端口')
    return d


def load_settings():
    return validate_config(json.loads(CONFIG.read_text()) if CONFIG.exists() else {})


def resolve_target(config, discover):
    """Automatic discovery must not be shadowed by a previous cap's IP."""
    if config['auto_discover']:
        target = discover()
        if target is None:
            raise RuntimeError('自动发现未找到脑电帽。请检查新帽子的网络接入；电脑网口有链路不代表帽子已联网。也可关闭自动发现并填写新帽子的 IP/端口。')
        return target
    if config['address'] and config['port']:
        return config['address'], config['port']
    raise RuntimeError('手动连接需填写当前脑电帽的 IP 和 TCP 端口')


class BrainCoSource:
    ready_timeout = 100

    def __init__(self, config=None, python_executable=None):
        self.config = load_settings() if config is None else validate_config(config)
        self.python_executable = python_executable or sys.executable
        self.archive_root = ROOT/'recordings'/'wire_archives'
        try:
            if python_executable:
                probe = subprocess.run([self.python_executable, '-c',
                    "import importlib.metadata;print(importlib.metadata.version('bc-ecap-sdk'))"],
                    capture_output=True, text=True, timeout=15)
                if probe.returncode:
                    raise RuntimeError('BrainCo 专用环境未安装 bc-ecap-sdk：' + probe.stderr[-1000:])
                self.version = probe.stdout.strip()
            else:
                self.version = importlib.metadata.version('bc-ecap-sdk')
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError('缺少 BrainCo SDK：Windows 请运行迁移包 INSTALL_WINDOWS.bat；原 Mac 环境运行 install_brainco.sh') from exc
        from sample_clock import SampleClock
        self.sample_clock = SampleClock(self.config['sample_rate'])
        self.process = None
        self.thread = None
        self.finished = threading.Event()
        self.stopping = False
        self.stopped = False
        # Full raw TCP is already archived. Bound the diagnostic snapshot for multi-hour sessions.
        self.records = deque(maxlen=2048)
        self.records_total = 0
        self.records_lock = threading.Lock()

    def describe(self):
        return dict(real=True, sample_rate=self.config['sample_rate'], channels=[f'Ch{i}' for i in range(1,33)],
                    host_clock='time.perf_counter', nominal_timestamps=True, marker_clock='rolling_packet_arrival_v1',
                    units='sdk_native_unverified', sdk_version=self.version, config=self.config,
                    clock_description='estimated: first batch host receive anchor + device sample sequence / rate; '
                                      'sequence gaps preserved; absolute device/host latency unverified',
                    decoder='bc-bg1b-ten-slot-reorder-v2', channel_layout='device_counter_then_32_eeg',
                    reorder_window_seconds=.2, raw_wire_archived=True,
                    timestamp_quality='estimated_unverified', packet_loss_detection=False,
                    sample_loss_detection=True)

    def start(self, on_sample, on_error):
        if self.process is not None:
            raise RuntimeError('BrainCo source 已启动，不可重复 start')
        env = dict(os.environ)
        self.archive = Path(self.archive_root)/uuid.uuid4().hex
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        env['BRAINCO_WIRE_ARCHIVE'] = str(self.archive)
        env['PYTHONPATH'] = str(ROOT / '.deps') + os.pathsep + str(ROOT / 'vendor')
        self.process = subprocess.Popen([self.python_executable, '-u', '-X', 'utf8', str(ROOT / 'brainco_worker.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True, bufsize=1, env=env)
        self.process.stdin.write(json.dumps(self.config) + '\n')
        self.process.stdin.flush()
        def receive():
            journal = None
            try:
                journal = (self.archive.parent/(self.archive.name+'-timing.jsonl')).open('x', encoding='utf-8')
                for line in self.process.stdout:
                    packet = json.loads(line)
                    if packet['type'] == 'batch':
                        values = packet.pop('values')
                        journal.write(json.dumps(packet)+'\n'); journal.flush()
                        anchor = packet.get('packet_anchor')
                        if anchor:
                            self.sample_clock.observe(anchor['index'], anchor['received'])
                        with self.records_lock:
                            self.records.append(packet)
                            self.records_total += 1
                        start, rate = packet['estimated_start'], self.config['sample_rate']
                        offsets = packet.get('sample_offsets', list(range(len(values))))
                        if len(offsets) != len(values):
                            raise ValueError('BrainCo 样本序号与通道行数不匹配')
                        indices = packet.get('device_sample_indices')
                        if indices is not None and len(indices) != len(values):
                            raise ValueError('BrainCo 设备样本序号长度与数据不匹配')
                        for i, row in enumerate(values):
                            on_sample(start+offsets[i]/rate, row, indices[i] if indices is not None else None)
                    elif packet['type'] == 'error':
                        with self.records_lock:
                            self.records.append(packet)
                            self.records_total += 1
                        on_error(packet['message'])
                    else:
                        with self.records_lock:
                            self.records.append(packet)
                            self.records_total += 1
                if not self.stopping:
                    on_error('BrainCo 采集子进程已退出；请查看连接/SDK 错误')
            except Exception as exc:
                on_error('BrainCo 数据通道错误：' + str(exc))
            finally:
                if journal is not None: journal.close()
                self.finished.set()
        self.thread = threading.Thread(target=receive, daemon=True, name='brainco-reader')
        self.thread.start()

    def diagnostics(self):
        with self.records_lock:
            return dict(timestamp_quality='estimated_unverified', packet_loss_detection=False,
                        sample_loss_detection=True,
                        records=list(self.records), records_total=self.records_total,
                        records_complete=self.records_total<=len(self.records),
                        raw_wire_archive=str(self.archive) if hasattr(self, 'archive') else None)

    def stop(self):
        if self.process is None or self.stopped:
            return
        process = self.process
        self.stopping = True
        if process.poll() is None:
            try:
                process.stdin.write('{"stop":true}\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                self.stopped = True
                raise RuntimeError('BrainCo SDK 停止超时，已关闭本次专属采集子进程；该轮需要检查')
        if self.thread:
            self.thread.join(2)
            if self.thread.is_alive():
                raise RuntimeError('BrainCo 数据读取线程未正常结束')
        self.stopped = True
        process.stdin.close()
        process.stdout.close()
        if process.returncode:
            raise RuntimeError(f'BrainCo 采集子进程异常退出（code={process.returncode}）')
