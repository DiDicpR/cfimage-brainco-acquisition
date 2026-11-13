from __future__ import annotations

from enum import StrEnum


class ExperimentState(StrEnum):
    IDLE = "idle"
    BASELINE_EYES_OPEN = "baseline_eyes_open"
    BASELINE_TRANSITION = "baseline_transition"
    BASELINE_EYES_CLOSED = "baseline_eyes_closed"
    BLOCK = "block"
    REST = "rest"
    COMPLETE = "complete"
    ERROR = "error"


class ExperimentStateMachine:
    """Explicit state holder; timing and device side effects belong to the runner."""

    def __init__(self) -> None:
        self.state = ExperimentState.IDLE

    def transition(self, next_state: ExperimentState) -> ExperimentState:
        self.state = next_state
        return self.state
