from __future__ import annotations

import json
import sqlite3
import zipfile
from collections import defaultdict
from pathlib import Path

from ..experiment.stimulus_manifest import Stimulus
from ..storage.database import sha256_file


def audit_session(
    database: sqlite3.Connection,
    *,
    session_id: str,
    session_dir: Path,
    run_mode: str,
) -> dict[str, object]:
    failures: list[str] = []
    warnings: list[str] = []
    session = database.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if session is None:
        raise ValueError(f"Unknown session: {session_id}")

    trials = list(
        database.execute(
            "SELECT * FROM trials WHERE session_id = ? ORDER BY global_index", (session_id,)
        )
    )
    plan = json.loads((session_dir / "trial_plan.json").read_text(encoding="utf-8"))
    planned = plan["trials"]
    if len(planned) != len(trials):
        failures.append("trial_plan_count_mismatch")
    else:
        for expected, actual in zip(planned, trials, strict=True):
            if expected["image_id"] != actual["image_id"]:
                failures.append("trial_plan_identity_mismatch")
                break
            if int(expected["repetition_index"]) != int(actual["repetition_index"]):
                failures.append("trial_plan_repetition_mismatch")
                break

    events = list(
        database.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY event_seq", (session_id,)
        )
    )
    if [row["event_seq"] for row in events] != list(range(1, len(events) + 1)):
        failures.append("event_sequence_not_contiguous")

    # Build lookups once, not a full events scan and SQL join per valid trial.
    types_by_trial: dict[str, set[str]] = defaultdict(set)
    event_trials = {}
    for event in events:
        types_by_trial[event["trial_id"]].add(event["event_type"])
        if event["event_type"] in {"IMAGE_ONSET", "IMAGE_OFFSET"}:
            event_trials[event["event_id"]] = event["trial_id"]
    aligned_by_trial: dict[str, int] = defaultdict(int)
    if run_mode != "simulation":
        for sync in database.execute(
            "SELECT event_id, sync_quality FROM eeg_sync WHERE event_id IN "
            "(SELECT event_id FROM events WHERE session_id = ?)", (session_id,)
        ):
            if sync["event_id"] in event_trials and sync["sync_quality"] in {
                "hardware_verified", "software_estimated"
            }:
                aligned_by_trial[event_trials[sync["event_id"]]] += 1
    required = {"IMAGE_ONSET", "IMAGE_OFFSET", "ISI_ONSET", "ISI_OFFSET"}
    for trial in trials:
        if trial["status"] != "valid":
            continue
        event_types = types_by_trial[trial["trial_id"]]
        if not required <= event_types:
            failures.append(f"valid_trial_missing_events:{trial['trial_id']}")
        if run_mode != "simulation":
            aligned = aligned_by_trial[trial["trial_id"]]
            if aligned != 2:
                failures.append(f"valid_trial_missing_alignment:{trial['trial_id']}")

    participant_id = session["participant_id"]
    expected_progress = {
        row["image_id"]: int(row["count"])
        for row in database.execute(
            """
            SELECT image_id, COUNT(*) AS count FROM trials t JOIN sessions s USING(session_id)
            WHERE s.participant_id = ? AND t.status = 'valid' GROUP BY image_id
            """,
            (participant_id,),
        )
    }
    durable_progress = {
        row["image_id"]: int(row["valid_repetition_count"])
        for row in database.execute(
            "SELECT image_id, valid_repetition_count FROM stimulus_progress WHERE participant_id = ?",
            (participant_id,),
        )
    }
    if expected_progress != durable_progress:
        failures.append("stimulus_progress_mismatch")

    required_files = {
        "session.json",
        "trial_plan.json",
        "events.jsonl",
        "metadata.toml",
        "qc_summary.json",
        "qc_report.md",
        "progress.tsv",
        "locked_config.json",
        "stimulus_manifest.csv",
    }
    missing = sorted(name for name in required_files if not (session_dir / name).is_file())
    if missing:
        failures.extend(f"missing_file:{name}" for name in missing)
    if run_mode != "simulation":
        recording = database.execute(
            "SELECT edf_path, status FROM eeg_recordings WHERE session_id = ?", (session_id,)
        ).fetchone()
        if recording is None or recording["status"] != "complete":
            failures.append("complete_eeg_recording_missing")

    if not events:
        warnings.append("session_has_no_events")
    return {
        "session_id": session_id,
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "warnings": warnings,
        "trial_count": len(trials),
        "event_count": len(events),
    }


def validate_participant_manifest(
    database: sqlite3.Connection,
    *,
    participant_id: str,
    dataset_root: Path,
    current_sha256: str,
    current_stimuli: list[Stimulus] | None = None,
    allow_identity_preserving_change: bool = False,
) -> None:
    current = {s.image_id: (s.file_path.resolve(), s.concept_id, s.category_id)
               for s in current_stimuli or []}
    current_paths = {identity[0]: image_id for image_id, identity in current.items()}
    if allow_identity_preserving_change and (not current or len(current_paths) != len(current)):
        raise ValueError("Identity-preserving manifest changes require unique image paths")
    rows = database.execute(
        "SELECT plan_path FROM sessions WHERE participant_id = ? ORDER BY start_time",
        (participant_id,),
    )
    for row in rows:
        plan_path = dataset_root / row["plan_path"]
        locked_path = plan_path.parent / "locked_config.json"
        if not locked_path.is_file():
            if allow_identity_preserving_change:
                raise ValueError(f"Missing historical manifest lock: {locked_path}")
            continue
        locked = json.loads(locked_path.read_text(encoding="utf-8"))
        previous_sha256 = locked.get("stimulus_manifest_sha256")
        if previous_sha256 is not None and previous_sha256 != current_sha256:
            if allow_identity_preserving_change:
                # Historical plans contain resolved paths; copied CSV paths are relative
                # to their original manifest directory, not the session directory.
                previous = json.loads(plan_path.read_text(encoding="utf-8"))
                for trial in previous["trials"]:
                    image_id = trial["image_id"]
                    identity = (Path(trial["file_path"]).resolve(), trial["concept_id"], trial["category_id"])
                    if image_id in current and current[image_id] != identity:
                        raise ValueError(f"Image identity changed for {image_id}; cannot reuse progress")
                    if identity[0] in current_paths and current_paths[identity[0]] != image_id:
                        raise ValueError(f"Historical image assigned a new ID: {identity[0]}")
                continue
            raise ValueError(
                f"Stimulus manifest changed for participant {participant_id}; "
                "start a new participant namespace or restore the locked manifest"
            )


def verify_checksums(session_dir: Path) -> list[str]:
    failures: list[str] = []
    checksum_path = session_dir / "checksums.sha256"
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", maxsplit=1)
        path = session_dir / relative
        if not path.is_file() or sha256_file(path) != expected:
            failures.append(relative)
    return failures


def export_session_package(session_dir: Path, export_root: Path) -> Path:
    export_root.mkdir(parents=True, exist_ok=True)
    output = export_root / f"{session_dir.parent.name}_{session_dir.name}.zip"
    partial = output.with_suffix(".zip.partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(session_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(session_dir.parent.parent))
    with zipfile.ZipFile(partial) as archive:
        bad_file = archive.testzip()
        if bad_file is not None:
            raise RuntimeError(f"Export archive CRC check failed: {bad_file}")
    partial.replace(output)
    return output
