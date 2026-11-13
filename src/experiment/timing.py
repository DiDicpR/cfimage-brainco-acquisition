from __future__ import annotations

import time


def monotonic_timestamp_ns() -> int:
    """Return the only clock used for event alignment inside a running session."""
    return time.monotonic_ns()
