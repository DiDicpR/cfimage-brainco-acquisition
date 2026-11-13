from __future__ import annotations

from pathlib import Path


def write_summary(path: Path, *, total_trials: int, valid_trials: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ratio = valid_trials / total_trials if total_trials else 0.0
    path.write_text(f"total_trials={total_trials}\nvalid_trials={valid_trials}\nvalid_ratio={ratio:.4f}\n", encoding="utf-8")
