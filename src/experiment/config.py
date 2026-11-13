from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise TypeError(f"Configuration root must be a mapping: {path}")
    return data


def config_digest(config: dict[str, Any]) -> str:
    import hashlib
    import json

    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_config(experiment: dict[str, Any], devices: dict[str, Any]) -> None:
    pause = experiment.get('presentation_break', {})
    if pause.get('enabled'):
        if not experiment.get('fixed_schedule'):
            raise ValueError('presentation_break requires the fixed schedule.')
        if (pause.get('every_images'), pause.get('question_duration_ms'), pause.get('resume_delay_ms')) != (80 if experiment.get('fixed_schedule', {}).get('profile') == 'brainco_all_images' else 50, 2000, 800):
            raise ValueError('休息批量必须符合方案，题目2000 ms、空格后800 ms。')
    if experiment.get('fixed_schedule'):
        from .fixed_schedule import validate_settings
        validate_settings(experiment)
    required = {"paths", "stimulus", "baseline", "repetition", "attention_probe", "blocks"}
    missing = required - experiment.keys()
    if missing:
        raise ValueError(f"Experiment configuration is missing sections: {sorted(missing)}")

    stimulus = experiment["stimulus"]
    minimum, maximum = stimulus["allowed_duration_ms"]
    for key in ("block_fixation_duration_ms", "image_duration_ms", "isi_ms"):
        value = int(stimulus[key])
        if not minimum <= value <= maximum:
            raise ValueError(f"stimulus.{key} must be within allowed_duration_ms")

    dual = experiment.get('dual_image', {})
    if dual.get('enabled', False):
        import re
        if int(stimulus['image_duration_ms']) != 1200:
            raise ValueError('双图范式的纯刺激时间必须为 1200 ms。')
        for name in ('cue_duration_ms', 'fixation_duration_ms'):
            if int(dual.get(name, 0)) < 1:
                raise ValueError(f'dual_image.{name} must be positive')
        if not re.fullmatch(r'[A-Za-z0-9]+', str(dual.get('task_label', ''))):
            raise ValueError('双图 task_label 只能包含英文字母和数字。')
        key = str(dual.get('report_error_key', '')).lower()
        probe_keys = {str(experiment['attention_probe'].get(k, '')).lower()
                      for k in ('key_correct', 'key_incorrect')}
        if len(key) != 1 or key in probe_keys or key.isspace():
            raise ValueError('看错报告键须为单个字符，不能与注意力题按键重复。')
        for name in ('target_repetitions', 'test_repetitions'):
            value = int(experiment['collection'].get(name, 4))
            if value < 2 or value % 2:
                raise ValueError('双图目标有效次数必须为正偶数，以保证左右各半。')

    baseline = experiment["baseline"]
    if any(int(baseline[key]) < 0 for key in ("eyes_open_sec", "transition_sec", "eyes_closed_sec")):
        raise ValueError("baseline durations must be non-negative")

    display = experiment.get("display", {})
    screen_index = display.get("screen_index", 0)
    if isinstance(screen_index, bool) or not isinstance(screen_index, int) or screen_index < 0:
        raise ValueError("display.screen_index must be a non-negative integer")

    if int(experiment["repetition"]["minimum_valid_repetitions"]) < 1:
        raise ValueError("minimum_valid_repetitions must be positive")

    blocks = experiment["blocks"]
    collection = experiment.get('collection', {})
    if collection.get('enabled', False) and collection.get('strategy') == 'scenes':
        from .scenes import settings
        settings(experiment)
    elif collection.get('enabled', False) and collection.get('strategy') == 'positions':
        from .positions import settings
        settings(experiment)
    elif collection.get('enabled', False):
        count = int(collection.get('group_count', 10))
        if not 1 <= count <= 100:
            raise ValueError('collection.group_count must be between 1 and 100')
        if not 0 <= int(collection.get('group_index', 0)) <= count:
            raise ValueError('collection.group_index must be 0 (auto) or a configured group')
        if not 1 <= int(collection.get('max_blocks_per_session', 5)) <= 100:
            raise ValueError('collection.max_blocks_per_session must be between 1 and 100')
    if int(blocks["trials_per_block"]) < 1 or int(blocks["session_blocks"]) < 1:
        raise ValueError("blocks.trials_per_block and blocks.session_blocks must be positive")
    if int(blocks["minimum_rest_sec"]) < 0:
        raise ValueError("blocks.minimum_rest_sec must be non-negative")
    if "auto_size_to_remaining" in blocks and not isinstance(blocks["auto_size_to_remaining"], bool):
        raise ValueError("blocks.auto_size_to_remaining must be boolean")

    robustness = experiment.get("robustness", {})
    for key in ("lock_configuration", "export_session_package", "verify_checksums_on_finish",
                "allow_identity_preserving_manifest_change"):
        if key in robustness and not isinstance(robustness[key], bool):
            raise ValueError(f"robustness.{key} must be boolean")

    probe = experiment["attention_probe"]
    if bool(probe["enabled"]):
        if "trials_per_probe" in probe and int(probe["trials_per_probe"]) < 1:
            raise ValueError("attention_probe.trials_per_probe must be positive")
        if int(probe["probes_per_full_session"]) < 0:
            raise ValueError("attention_probe.probes_per_full_session must be non-negative")
        if int(probe["minimum_trials_between_probes"]) < 1:
            raise ValueError("attention_probe.minimum_trials_between_probes must be positive")
        if int(probe["response_timeout_ms"]) < 1:
            raise ValueError("attention_probe.response_timeout_ms must be positive")
        if not str(probe.get("key_correct", "")).strip() or not str(probe.get("key_incorrect", "")).strip():
            raise ValueError("attention_probe requires key_correct and key_incorrect")
        if probe["key_correct"] == probe["key_incorrect"]:
            raise ValueError("attention_probe keys must be different")

    if "trigger" not in devices or "codes" not in devices["trigger"]:
        raise ValueError("devices config must define trigger.codes")
    realtime = devices.get("devices", {}).get("realtime_simulation", {})
    if bool(realtime.get("enabled")):
        if realtime.get("device_type") not in {"neuracle", "brainco"}:
            raise ValueError("realtime_simulation.device_type must be neuracle or brainco")
        if int(realtime.get("sampling_rate_hz", 0)) < 1:
            raise ValueError("realtime_simulation.sampling_rate_hz must be positive")
        if int(realtime.get("channel_count", 0)) < 1:
            raise ValueError("realtime_simulation.channel_count must be positive")
    for name in ("neuracle", "brainco"):
        section = devices.get("devices", {}).get(name, {})
        simulated = bool(realtime.get("enabled")) and realtime.get("device_type") == name
        if bool(section.get("enabled")) and not simulated and not str(section.get("adapter_factory", "")).strip():
            raise ValueError(f"enabled {name} configuration requires adapter_factory")
    brainco = devices.get('devices', {}).get('brainco', {})
    if brainco.get('enabled') and 'brainco_native:' in brainco.get('adapter_factory',''):
        from ..acquisition_devices.brainco_native import source_config
        source_config(brainco)
    neuracle_active = bool(devices.get("devices", {}).get("neuracle", {}).get("enabled")) or (
        bool(realtime.get("enabled")) and realtime.get("device_type") == "neuracle"
    )
    neuracle = devices.get("devices", {}).get("neuracle", {})
    if "allow_software_trigger_fallback" in neuracle and not isinstance(
        neuracle["allow_software_trigger_fallback"], bool
    ):
        raise ValueError("devices.neuracle.allow_software_trigger_fallback must be boolean")
    for key in (
        "trigger_command_timeout_ms",
        "trigger_verification_timeout_ms",
        "stream_stall_timeout_ms",
        "trigger_poll_interval_ms",
        "trigger_failure_abort_count",
    ):
        if key in neuracle:
            try:
                positive = int(neuracle[key]) > 0
            except (TypeError, ValueError):
                positive = False
            if not positive:
                raise ValueError(f"devices.neuracle.{key} must be positive")
    if bool(neuracle.get("enabled")):
        trigger_transport = str(neuracle.get("trigger_transport", "serial")).lower()
        if trigger_transport not in {"serial", "tcp"}:
            raise ValueError("devices.neuracle.trigger_transport must be serial or tcp")
        acknowledgement = str(neuracle.get("trigger_acknowledgement", "dcp")).lower()
        if acknowledgement not in {"dcp", "trg"}:
            raise ValueError("devices.neuracle.trigger_acknowledgement must be dcp or trg")
        if acknowledgement == "trg" and (trigger_transport != "tcp" or str(
                neuracle.get("trigger_device", "triggerin")).lower() != "triggerbox"):
            raise ValueError("TRG acknowledgement requires TCP TriggerBox transport")
        if trigger_transport == "serial" and not str(
            neuracle.get("trigger_port", "")
        ).strip():
            raise ValueError("serial trigger transport requires devices.neuracle.trigger_port")
        if trigger_transport == "tcp":
            if str(neuracle.get("trigger_device", "triggerin")).lower() != "triggerbox":
                raise ValueError("TCP DCP trigger transport requires trigger_device=triggerbox")
            if not str(neuracle.get("trigger_host", "")).strip():
                raise ValueError("TCP DCP trigger transport requires trigger_host")
            try:
                trigger_tcp_port = int(neuracle.get("trigger_tcp_port", 4321))
            except (TypeError, ValueError):
                trigger_tcp_port = 0
            if trigger_tcp_port <= 0:
                raise ValueError("TCP DCP trigger transport requires a positive trigger_tcp_port")
    if neuracle_active and not bool(
        devices["trigger"].get("enabled")
    ):
        raise ValueError("enabled neuracle configuration requires trigger.enabled=true")
