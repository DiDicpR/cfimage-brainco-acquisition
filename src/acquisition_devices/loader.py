from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

from .base import EEGDevice


def load_device_factory(specification: str, config: dict[str, Any]) -> EEGDevice:
    """Load a site-owned adapter factory expressed as ``module:callable``."""
    try:
        module_name, attribute = specification.split(":", maxsplit=1)
        factory: Callable[[dict[str, Any]], EEGDevice] = getattr(import_module(module_name), attribute)
    except (AttributeError, ImportError, ValueError) as error:
        raise ValueError(f"Invalid device adapter factory: {specification}") from error
    device = factory(config)
    if not isinstance(device, EEGDevice):
        raise TypeError(f"Device factory {specification} did not return an EEGDevice")
    return device


def available_device_modes(config: dict[str, Any], *, allow_simulation: bool = True) -> list[str]:
    modes = ["simulation"] if allow_simulation else []
    realtime = config["devices"].get("realtime_simulation", {})
    if bool(realtime.get("enabled")):
        modes.append(str(realtime["device_type"]))
    for name in ("neuracle", "brainco"):
        if bool(config["devices"].get(name, {}).get("enabled")) and name not in modes:
            modes.append(name)
    return modes


def create_configured_device(mode: str, config: dict[str, Any]) -> EEGDevice | None:
    if mode == "simulation":
        return None
    realtime = config["devices"].get("realtime_simulation", {})
    if bool(realtime.get("enabled")):
        from .realtime_simulation import RealtimeSimulatedDevice

        if realtime.get("device_type") != mode:
            raise ValueError(f"Realtime simulation is configured for {realtime.get('device_type')}, not {mode}")
        return RealtimeSimulatedDevice(mode, realtime)
    section = config["devices"][mode]
    if not bool(section.get("enabled")):
        raise ValueError(f"devices.{mode}.enabled must be true")
    return load_device_factory(str(section["adapter_factory"]), section)
