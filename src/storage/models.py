from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    participant_id: str
    day_index: int
    status: str
