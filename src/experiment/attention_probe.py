from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttentionResponse:
    response: str | None
    reaction_time_ms: int | None
    correct: bool
    timeout: bool


def score_response(response: str | None, expected: str, timeout: bool = False) -> AttentionResponse:
    return AttentionResponse(response, None, response == expected and not timeout, timeout)
