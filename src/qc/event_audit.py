from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventAuditResult:
    valid: bool
    reasons: tuple[str, ...] = ()


def audit_trial(*, displayed: bool, trigger_written: bool, device_healthy: bool) -> EventAuditResult:
    reasons = tuple(
        reason
        for reason, passed in (
            ("display_failed", displayed),
            ("trigger_missing", trigger_written),
            ("device_unhealthy", device_healthy),
        )
        if not passed
    )
    return EventAuditResult(not reasons, reasons)
