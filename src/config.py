"""配置加载与边界校验；相对路径以配置文件目录为基准。"""

from dataclasses import dataclass
import math
from pathlib import Path
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


@dataclass(frozen=True)
class Config:
    task_name: str
    trials: int
    duration_s: float
    sample_rate_hz: int
    channels: int
    output_dir: Path


@dataclass(frozen=True)
class BrainCoConfig(Config):
    backend: str
    protocol: dict
    assets: dict
    display: dict
    device_options: dict
    source_path: Path


def load_brainco(raw: dict, path: Path) -> BrainCoConfig:
    _keys(raw, {'experiment','device','storage','protocol','assets','display'}, 'root')
    e=_section(raw,'experiment',{'name','trials','trial_duration_s'})
    d=_section(raw,'device',{'backend','sample_rate_hz','channels','auto_discover','host','port','gain','scan_timeout','ready_timeout'})
    s=_section(raw,'storage',{'output_dir'})
    p=_section(raw,'protocol',{'participant','repetitions','isi_s','break_every','math_duration_s','resume_delay_s','seed','max_cycles_per_session'})
    a=_section(raw,'assets',{'manifest','schedule','manifest_sha256','schedule_sha256'})
    display=_section(raw,'display',{'screen_index','fullscreen'})
    if not isinstance(e.get('name'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',e['name']):
        raise ValueError('experiment.name invalid')
    for value,expected,name in [(e.get('trials'),25800,'trials'),(d.get('channels'),32,'channels'),(p.get('repetitions'),10,'repetitions'),(p.get('break_every'),80,'break_every'),(p.get('seed'),20261112,'seed')]:
        if type(value) is not int or value!=expected:raise ValueError(f'{name} must be {expected}')
    for value,expected,name in [(e.get('trial_duration_s'),.6,'trial_duration_s'),(p.get('isi_s'),.1,'isi_s'),(p.get('math_duration_s'),2,'math_duration_s'),(p.get('resume_delay_s'),.8,'resume_delay_s')]:
        if type(value) not in (int,float) or value!=expected:raise ValueError(f'{name} must be {expected}')
    rate=_integer(d.get('sample_rate_hz'),'sample_rate_hz',1,2000)
    if rate not in (250,500,1000,2000):raise ValueError('Unsupported BrainCo sample rate')
    _integer(d.get('port'),'port',0,65535)
    _integer(d.get('scan_timeout'),'scan_timeout',1,10)
    _integer(d.get('ready_timeout'),'ready_timeout',1,20)
    _integer(d.get('gain'),'gain',1,24)
    if d['gain'] not in (1,2,4,6,8,12,24):raise ValueError('Unsupported gain')
    if type(d.get('auto_discover')) is not bool or not isinstance(d.get('host'),str):raise ValueError('Invalid discovery settings')
    if not d['auto_discover'] and (not d['host'].strip() or not d['port']):raise ValueError('Manual device connection requires host and port')
    if not isinstance(p.get('participant'),str) or (p['participant'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',p['participant'])):raise ValueError('Invalid participant ID')
    _integer(p.get('max_cycles_per_session'),'max_cycles_per_session',0,10)
    _integer(display.get('screen_index'),'screen_index',0,15)
    if type(display.get('fullscreen')) is not bool:raise ValueError('fullscreen must be boolean')
    for name in ('manifest_sha256','schedule_sha256'):
        if not isinstance(a.get(name),str) or not re.fullmatch('[0-9a-f]{64}',a[name]):raise ValueError(f'Invalid {name}')
    base=path.resolve().parent
    for name in ('manifest','schedule'):
        if not isinstance(a.get(name),str) or not a[name].strip():raise ValueError(f'Invalid {name}')
        a[name]=(base/a[name]).resolve()
    if not isinstance(s.get('output_dir'),str) or not s['output_dir'].strip():raise ValueError('Invalid output_dir')
    return BrainCoConfig(e['name'],25800,.6,rate,32,(base/s['output_dir']).resolve(),'brainco',p,a,display,d,path.resolve())


def load_config(path: Path) -> Config:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    if isinstance(raw.get('device'),dict) and raw['device'].get('backend')=='brainco':
        return load_brainco(raw,path)
    _keys(raw, {"experiment", "device", "storage"}, "root")
    experiment = _section(raw, "experiment", {"name", "trials", "trial_duration_s"})
    device = _section(raw, "device", {"backend", "sample_rate_hz", "channels"})
    storage = _section(raw, "storage", {"output_dir"})
    name = experiment.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("experiment.name 必须是 1–64 位字母、数字、下划线或连字符")
    trials = _integer(experiment.get("trials"), "experiment.trials", 1, 10000)
    rate = _integer(device.get("sample_rate_hz"), "device.sample_rate_hz", 1, 10000)
    channels = _integer(device.get("channels"), "device.channels", 1, 256)
    duration = experiment.get("trial_duration_s")
    if (type(duration) not in (int, float) or not math.isfinite(duration)
            or not 0 < duration <= 3600):
        raise ValueError("experiment.trial_duration_s 必须是 (0, 3600] 内的有限数值")
    if duration * rate < 1:
        raise ValueError("每个试次至少应包含一个采样点")
    if device.get("backend") != "mock":
        raise ValueError("当前模板只实现 mock；真实设备需实现并注册适配器")
    output = storage.get("output_dir")
    if not isinstance(output, str) or not output.strip():
        raise ValueError("storage.output_dir 必须是非空路径")
    return Config(name, trials, float(duration), rate, channels,
                  (path.resolve().parent / output).resolve())


def _keys(value: dict, allowed: set[str], label: str) -> None:
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"{label} 含未知配置项: {', '.join(sorted(unknown))}")


def _section(raw: dict, name: str, allowed: set[str]) -> dict:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"缺少 [{name}] 配置节")
    _keys(value, allowed, name)
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} 必须是 [{minimum}, {maximum}] 内的整数")
    return value
