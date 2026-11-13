from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BaselinePhase:
    name: str
    duration_sec: int


def baseline_phases(config: dict) -> list[BaselinePhase]:
    baseline = config["baseline"]
    return [
        BaselinePhase("eyes_open", baseline["eyes_open_sec"]),
        BaselinePhase("transition", baseline["transition_sec"]),
        BaselinePhase("eyes_closed", baseline["eyes_closed_sec"]),
    ]
