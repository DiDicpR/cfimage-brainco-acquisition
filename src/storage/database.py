from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..acquisition_devices.base import RecordingArtifact, SyncPoint
from ..experiment.planner import PlannedTrial, SessionPlan

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  participant_id TEXT NOT NULL,
  day_index INTEGER NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT,
  config_hash TEXT NOT NULL,
  random_seed INTEGER NOT NULL,
  plan_path TEXT NOT NULL,
  device_name TEXT NOT NULL,
  run_mode TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
  block_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  block_index INTEGER NOT NULL,
  planned_trials INTEGER NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trials (
  trial_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  block_id TEXT NOT NULL REFERENCES blocks(block_id),
  image_id TEXT NOT NULL,
  repetition_index INTEGER NOT NULL,
  global_index INTEGER NOT NULL,
  status TEXT NOT NULL,
  planned_onset_ns INTEGER,
  actual_onset_ns INTEGER,
  actual_offset_ns INTEGER,
  planned_frame_count INTEGER NOT NULL,
  actual_frame_count INTEGER,
  trigger_status TEXT NOT NULL,
  validity TEXT,
  invalid_reason TEXT,
  run_mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_seq INTEGER NOT NULL,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  trial_id TEXT,
  event_type TEXT NOT NULL,
  timestamp_ns INTEGER NOT NULL,
  frame_index INTEGER,
  trigger_code INTEGER,
  metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, event_seq);
CREATE TABLE IF NOT EXISTS attention_responses (
  response_id INTEGER PRIMARY KEY AUTOINCREMENT,
  trial_id TEXT NOT NULL REFERENCES trials(trial_id),
  question_type TEXT NOT NULL,
  question_text TEXT,
  statement_is_correct INTEGER,
  response TEXT,
  reaction_time_ms INTEGER,
  correct INTEGER NOT NULL,
  timeout INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stimulus_progress (
  participant_id TEXT NOT NULL,
  image_id TEXT NOT NULL,
  valid_repetition_count INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (participant_id, image_id)
);
CREATE TABLE IF NOT EXISTS device_logs (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  device_type TEXT NOT NULL,
  state TEXT NOT NULL,
  timestamp_ns INTEGER NOT NULL,
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eeg_recordings (
  recording_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  device_type TEXT NOT NULL,
  source_transport TEXT NOT NULL,
  edf_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  file_size_bytes INTEGER NOT NULL,
  sample_count INTEGER,
  sampling_rate_hz REAL,
  channel_count INTEGER,
  recording_start_ns INTEGER,
  recording_end_ns INTEGER,
  reference TEXT,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eeg_channels (
  recording_id TEXT NOT NULL REFERENCES eeg_recordings(recording_id),
  channel_index INTEGER NOT NULL,
  channel_name TEXT NOT NULL,
  unit TEXT NOT NULL,
  PRIMARY KEY (recording_id, channel_index)
);
CREATE TABLE IF NOT EXISTS eeg_sync (
  event_id INTEGER NOT NULL REFERENCES events(event_id),
  recording_id TEXT,
  eeg_sample_index INTEGER,
  eeg_timestamp_ns INTEGER,
  alignment_method TEXT NOT NULL,
  sync_quality TEXT NOT NULL,
  latency_ns INTEGER,
  failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS data_gaps (
  gap_id INTEGER PRIMARY KEY AUTOINCREMENT,
  recording_id TEXT NOT NULL REFERENCES eeg_recordings(recording_id),
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  device_type TEXT NOT NULL,
  start_sample_index INTEGER NOT NULL,
  end_sample_index INTEGER NOT NULL,
  missing_samples INTEGER NOT NULL,
  start_timestamp_ns INTEGER NOT NULL,
  end_timestamp_ns INTEGER NOT NULL,
  duration_ms REAL NOT NULL,
  reason TEXT NOT NULL,
  recovered INTEGER NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(SCHEMA)
    _add_attention_question_columns(connection)
    _add_real_recording_columns(connection)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_eeg_sync_event ON eeg_sync(event_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_trials_session ON trials(session_id)")
    connection.commit()
    return connection


def _add_attention_question_columns(connection: sqlite3.Connection) -> None:
    """Upgrade databases created before arithmetic probes were introduced."""

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(attention_responses)")}
    for name, definition in {
        "question_text": "TEXT",
        "statement_is_correct": "INTEGER",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE attention_responses ADD COLUMN {name} {definition}")


def _add_real_recording_columns(connection: sqlite3.Connection) -> None:
    additions = {
        "eeg_recordings": {
            "recording_start_ns": "INTEGER",
            "recording_end_ns": "INTEGER",
            "reference": "TEXT",
        },
        "eeg_sync": {
            "eeg_timestamp_ns": "INTEGER",
            "latency_ns": "INTEGER",
            "failure_reason": "TEXT",
        },
    }
    for table, definitions in additions.items():
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in definitions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


class SessionStore:
    """Owns the durable artifacts of one session.

    SQLite is authoritative for plan, trial state and progress. JSONL mirrors
    events so an experimenter can inspect a session without querying SQLite.
    """

    def __init__(
        self,
        database: sqlite3.Connection,
        *,
        dataset_root: Path,
        participant_id: str,
        session_id: str,
        day_index: int,
        config_hash: str,
        random_seed: int,
        run_mode: str,
        locked_config: dict[str, Any] | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        self.database = database
        self.dataset_root = dataset_root.resolve()
        self.participant_id = participant_id
        self.session_id = session_id
        self.day_index = day_index
        self.config_hash = config_hash
        self.random_seed = random_seed
        self.run_mode = run_mode
        self.locked_config = locked_config or {}
        self.manifest_path = manifest_path
        self.session_dir = self.dataset_root / participant_id / session_id
        self.events_path = self.session_dir / "events.jsonl"
        self.plan_path = self.session_dir / "trial_plan.json"
        self._event_seq = 0

    @property
    def relative_session_dir(self) -> str:
        return self.session_dir.relative_to(self.dataset_root).as_posix()

    def create(self, plan: SessionPlan, *, device_name: str = "simulation") -> None:
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self.events_path.touch()
        self.plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        (self.session_dir / "locked_config.json").write_text(
            json.dumps(self.locked_config, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if self.manifest_path is not None:
            shutil.copyfile(self.manifest_path, self.session_dir / "stimulus_manifest.csv")
        session_json = {
            "session_id": self.session_id,
            "participant_id": self.participant_id,
            "day_index": self.day_index,
            "run_mode": self.run_mode,
            "device_name": device_name,
            "config_hash": self.config_hash,
            "random_seed": self.random_seed,
            "status": "running",
            "started_at": utc_now(),
        }
        (self.session_dir / "session.json").write_text(
            json.dumps(session_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.session_dir / "metadata.toml").write_text(
            "\n".join(
                [
                    f'session_id = "{self.session_id}"',
                    f'participant_id = "{self.participant_id}"',
                    f'run_mode = "{self.run_mode}"',
                    f"eeg_available = {'false' if self.run_mode == 'simulation' else 'true'}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        with self.database:
            self.database.execute(
                """
                INSERT INTO sessions(session_id, participant_id, day_index, start_time, config_hash,
                                     random_seed, plan_path, device_name, run_mode, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    self.session_id,
                    self.participant_id,
                    self.day_index,
                    utc_now(),
                    self.config_hash,
                    self.random_seed,
                    self.plan_path.relative_to(self.dataset_root).as_posix(),
                    device_name,
                    self.run_mode,
                ),
            )
            for block_index in range(1, plan.block_count + 1):
                block_trials = plan.by_block(block_index)
                self.database.execute(
                    "INSERT INTO blocks VALUES (?, ?, ?, ?, 'planned')",
                    (self.block_id(block_index), self.session_id, block_index, len(block_trials)),
                )
            for trial in plan.trials:
                self.database.execute(
                    """
                    INSERT INTO trials(trial_id, session_id, block_id, image_id, repetition_index,
                                       global_index, status, planned_frame_count, trigger_status, run_mode)
                    VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, 'not_required', ?)
                    """,
                    (
                        self.trial_id(trial),
                        self.session_id,
                        self.block_id(trial.block_index),
                        trial.image_id,
                        trial.repetition_index,
                        trial.global_index,
                        0,
                        self.run_mode,
                    ),
                )

    def block_id(self, block_index: int) -> str:
        return f"{self.session_id}_block-{block_index:02d}"

    def trial_id(self, trial: PlannedTrial) -> str:
        return f"{self.session_id}_trial-{trial.global_index:04d}"

    def progress(self) -> dict[str, int]:
        rows = self.database.execute(
            "SELECT image_id, valid_repetition_count FROM stimulus_progress WHERE participant_id = ?",
            (self.participant_id,),
        )
        return {row["image_id"]: int(row["valid_repetition_count"]) for row in rows}

    def completed_baseline_session_for_day(self, *, local_day: date, timezone_name: str) -> str | None:
        """Return a completed same-day session with both required rest baselines."""

        timezone = ZoneInfo(timezone_name)
        rows = self.database.execute(
            """
            SELECT s.session_id, s.start_time
            FROM sessions AS s
            WHERE s.participant_id = ?
              AND s.run_mode = ?
              AND s.status = 'complete'
              AND EXISTS (
                SELECT 1 FROM events AS e
                WHERE e.session_id = s.session_id AND e.event_type = 'BASELINE_EYES_OPEN_END'
              )
              AND EXISTS (
                SELECT 1 FROM events AS e
                WHERE e.session_id = s.session_id AND e.event_type = 'BASELINE_EYES_CLOSED_END'
              )
            ORDER BY s.start_time DESC
            """,
            (self.participant_id, self.run_mode),
        )
        for row in rows:
            started_at = datetime.fromisoformat(str(row["start_time"]))
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            if started_at.astimezone(timezone).date() == local_day:
                return str(row["session_id"])
        return None

    def record_event(
        self,
        event_type: str,
        *,
        trial: PlannedTrial | None = None,
        frame_index: int | None = None,
        trigger_code: int | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp_ns: int | None = None,
    ) -> int:
        self._event_seq += 1
        event = {
            "event_seq": self._event_seq,
            "session_id": self.session_id,
            "trial_id": self.trial_id(trial) if trial else None,
            "event_type": event_type,
            "timestamp_ns": timestamp_ns if timestamp_ns is not None else time.monotonic_ns(),
            "frame_index": frame_index,
            "trigger_code": trigger_code,
            "metadata": metadata or {},
        }
        for name in (
            "eeg_sample_index",
            "eeg_timestamp_ns",
            "alignment_method",
            "sync_quality",
            "latency_ns",
            "failure_reason",
        ):
            if name in event["metadata"]:
                event[name] = event["metadata"][name]
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        with self.database:
            cursor = self.database.execute(
                """
                INSERT INTO events(event_seq, session_id, trial_id, event_type, timestamp_ns,
                                   frame_index, trigger_code, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_seq"],
                    event["session_id"],
                    event["trial_id"],
                    event["event_type"],
                    event["timestamp_ns"],
                    event["frame_index"],
                    event["trigger_code"],
                    json.dumps(event["metadata"], ensure_ascii=False),
                ),
            )
        return int(cursor.lastrowid)

    def record_sync(self, event_id: int, sync: SyncPoint, *, recording_id: str | None = None) -> None:
        with self.database:
            self.database.execute(
                """
                INSERT INTO eeg_sync(event_id, recording_id, eeg_sample_index, eeg_timestamp_ns,
                                     alignment_method, sync_quality, latency_ns, failure_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    recording_id,
                    sync.sample_index,
                    sync.eeg_timestamp_ns,
                    sync.alignment_method,
                    sync.quality,
                    sync.latency_ns,
                    sync.failure_reason,
                ),
            )

    def register_recording(self, device_name: str, artifact: RecordingArtifact) -> str:
        path = artifact.path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"EEG recording was not written: {path}")
        try:
            relative_path = path.relative_to(self.dataset_root).as_posix()
        except ValueError as error:
            raise ValueError("EEG recording must be stored below dataset_root") from error
        recording_id = f"{self.session_id}_recording-01"
        digest = sha256_file(path)
        with self.database:
            self.database.execute(
                """
                INSERT INTO eeg_recordings(recording_id, session_id, device_type, source_transport, edf_path,
                                           sha256, file_size_bytes, sample_count, sampling_rate_hz, channel_count,
                                           recording_start_ns, recording_end_ns, reference, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
                """,
                (
                    recording_id,
                    self.session_id,
                    device_name,
                    artifact.source_transport,
                    relative_path,
                    digest,
                    path.stat().st_size,
                    artifact.sample_count,
                    artifact.sampling_rate_hz,
                    artifact.channel_count,
                    artifact.recording_start_ns,
                    artifact.recording_end_ns,
                    artifact.reference,
                ),
            )
            self.database.executemany(
                "INSERT INTO eeg_channels(recording_id, channel_index, channel_name, unit) VALUES (?, ?, ?, ?)",
                [
                    (recording_id, index, name, artifact.channel_units[index] if artifact.channel_units else "unknown")
                    for index, name in enumerate(artifact.channel_names)
                ],
            )
            self.database.executemany(
                """
                INSERT INTO data_gaps(recording_id, session_id, device_type, start_sample_index,
                                      end_sample_index, missing_samples, start_timestamp_ns, end_timestamp_ns,
                                      duration_ms, reason, recovered)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        recording_id,
                        self.session_id,
                        device_name,
                        gap.start_sample_index,
                        gap.end_sample_index,
                        gap.missing_samples,
                        gap.start_timestamp_ns,
                        gap.end_timestamp_ns,
                        (gap.end_timestamp_ns - gap.start_timestamp_ns) / 1_000_000,
                        gap.reason,
                        int(gap.recovered),
                    )
                    for gap in artifact.gaps
                ],
            )
            self.database.execute(
                """
                UPDATE eeg_sync SET recording_id = ?
                WHERE recording_id IS NULL AND event_id IN (
                    SELECT event_id FROM events WHERE session_id = ?
                )
                """,
                (recording_id, self.session_id),
            )
        return recording_id

    def record_device_log(self, device_type: str, state: str, message: str) -> None:
        with self.database:
            self.database.execute(
                "INSERT INTO device_logs(session_id, device_type, state, timestamp_ns, message) VALUES (?, ?, ?, ?, ?)",
                (self.session_id, device_type, state, time.monotonic_ns(), message),
            )

    def mark_presented(self, trial: PlannedTrial, *, onset_ns: int, planned_frames: int) -> None:
        with self.database:
            self.database.execute(
                """
                UPDATE trials SET status = 'presented', actual_onset_ns = ?, planned_frame_count = ?
                WHERE trial_id = ?
                """,
                (onset_ns, planned_frames, self.trial_id(trial)),
            )

    def finalize_trial(
        self,
        trial: PlannedTrial,
        *,
        valid: bool,
        actual_offset_ns: int,
        actual_frames: int,
        reason: str | None = None,
    ) -> None:
        trial_id = self.trial_id(trial)
        status = "valid" if valid else "invalid"
        with self.database:
            updated = self.database.execute(
                """
                UPDATE trials
                SET status = ?, validity = ?, invalid_reason = ?, actual_offset_ns = ?, actual_frame_count = ?
                WHERE trial_id = ? AND status IN ('planned', 'presented')
                """,
                (status, status, reason, actual_offset_ns, actual_frames, trial_id),
            )
            if valid and updated.rowcount:
                self.database.execute(
                    """
                    INSERT INTO stimulus_progress(participant_id, image_id, valid_repetition_count, updated_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(participant_id, image_id) DO UPDATE SET
                      valid_repetition_count = valid_repetition_count + 1,
                      updated_at = excluded.updated_at
                    """,
                    (self.participant_id, trial.image_id, utc_now()),
                )

    def record_attention(
        self,
        trial: PlannedTrial,
        *,
        response: str | None,
        reaction_time_ms: int | None,
        correct: bool,
        timeout: bool,
    ) -> None:
        with self.database:
            self.database.execute(
                """
                INSERT INTO attention_responses(
                    trial_id, question_type, question_text, statement_is_correct,
                    response, reaction_time_ms, correct, timeout
                )
                VALUES (?, 'arithmetic_verification', ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.trial_id(trial),
                    trial.arithmetic_expression,
                    int(bool(trial.arithmetic_is_correct)),
                    response,
                    reaction_time_ms,
                    int(correct),
                    int(timeout),
                ),
            )

    def set_block_status(self, block_index: int, status: str) -> None:
        with self.database:
            self.database.execute(
                "UPDATE blocks SET status = ? WHERE block_id = ?",
                (status, self.block_id(block_index)),
            )

    def finish(self, status: str, *, export: bool = True) -> None:
        ended_at = utc_now()
        with self.database:
            self.database.execute(
                "UPDATE sessions SET end_time = ?, status = ? WHERE session_id = ?",
                (ended_at, status, self.session_id),
            )
        self._write_session_metadata(status, ended_at)
        if export:
            self.export_finalized()

    def export_finalized(self) -> None:
        """Export a stopped session; safe with a separate worker-owned connection."""
        session = self.database.execute(
            "SELECT status, end_time FROM sessions WHERE session_id = ?", (self.session_id,)
        ).fetchone()
        if session is None or session["status"] == "running" or not session["end_time"]:
            raise RuntimeError("Cannot export a running session")
        status, ended_at = session["status"], session["end_time"]
        self._write_qc_summary(status)
        self._export_events_tsv()
        self._export_progress()
        if self.locked_config.get('experiment', {}).get('dual_image', {}).get('enabled', False):
            from .bids_package import export_bids_package
            export_bids_package(self)
        from ..qc.integrity import audit_session, export_session_package, verify_checksums

        integrity = audit_session(
            self.database,
            session_id=self.session_id,
            session_dir=self.session_dir,
            run_mode=self.run_mode,
        )
        (self.session_dir / "integrity_report.json").write_text(
            json.dumps(integrity, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        strict = bool(
            self.locked_config.get("experiment", {}).get("robustness", {}).get("lock_configuration", False)
        )
        if strict and integrity["status"] != "pass":
            with self.database:
                self.database.execute(
                    "UPDATE sessions SET status = 'integrity_failed' WHERE session_id = ?",
                    (self.session_id,),
                )
            self._write_session_metadata("integrity_failed", ended_at)
        self._write_checksums()
        checksum_failures = verify_checksums(self.session_dir)
        if checksum_failures:
            raise RuntimeError(f"Session checksum verification failed: {checksum_failures}")
        if (not self.locked_config.get('experiment', {}).get('dual_image', {}).get('enabled', False)
                and bool(self.locked_config.get("experiment", {}).get("robustness", {}).get("export_session_package", True))):
            export_session_package(self.session_dir, self.dataset_root / "exports")
        if strict and integrity["status"] != "pass":
            raise RuntimeError(f"Session integrity audit failed: {integrity['failures']}")

    def abort_unfinished(self, *, export: bool = True) -> None:
        with self.database:
            self.database.execute(
                """
                UPDATE trials SET status = 'aborted', validity = 'aborted', invalid_reason = 'session_interrupted'
                WHERE session_id = ? AND status IN ('planned', 'presented')
                """,
                (self.session_id,),
            )
        self.finish("incomplete", export=export)

    def _write_qc_summary(self, status: str) -> None:
        rows = self.database.execute(
            "SELECT status, COUNT(*) AS count FROM trials WHERE session_id = ? GROUP BY status",
            (self.session_id,),
        )
        counts = {row["status"]: int(row["count"]) for row in rows}
        attention = self.database.execute(
            """
            SELECT COUNT(*) AS total, COALESCE(SUM(correct), 0) AS correct, COALESCE(SUM(timeout), 0) AS timeout,
                   AVG(reaction_time_ms) AS mean_reaction_time_ms
            FROM attention_responses ar JOIN trials t ON ar.trial_id = t.trial_id
            WHERE t.session_id = ?
            """,
            (self.session_id,),
        ).fetchone()
        attention_summary = dict(attention) if attention else {"total": 0, "correct": 0, "timeout": 0}
        total_attention = int(attention_summary["total"])
        attention_summary["accuracy"] = (
            float(attention_summary["correct"]) / total_attention if total_attention else None
        )
        attention_summary["timeout_rate"] = (
            float(attention_summary["timeout"]) / total_attention if total_attention else None
        )
        invalid_reasons = {
            row["invalid_reason"]: int(row["count"])
            for row in self.database.execute(
                """
                SELECT invalid_reason, COUNT(*) AS count FROM trials
                WHERE session_id = ? AND invalid_reason IS NOT NULL
                GROUP BY invalid_reason
                """,
                (self.session_id,),
            )
        }
        timing_rows = self.database.execute(
            """
            SELECT planned_frame_count, actual_frame_count FROM trials
            WHERE session_id = ? AND actual_frame_count IS NOT NULL
            """,
            (self.session_id,),
        )
        planned = actual = mismatches = 0
        for row in timing_rows:
            expected = int(row["planned_frame_count"])
            observed = int(row["actual_frame_count"])
            planned += expected
            actual += observed
            mismatches += int(expected != observed)
        device_errors = self.database.execute(
            "SELECT COUNT(*) FROM device_logs WHERE session_id = ? AND state LIKE '%error%'",
            (self.session_id,),
        ).fetchone()[0]
        alignment_rows = self.database.execute(
            """
            SELECT sync_quality, COUNT(*) AS count FROM eeg_sync
            WHERE event_id IN (SELECT event_id FROM events WHERE session_id = ?)
            GROUP BY sync_quality
            """,
            (self.session_id,),
        )
        alignment_quality = {row["sync_quality"]: int(row["count"]) for row in alignment_rows}
        alignment_total = sum(alignment_quality.values())
        aligned = sum(alignment_quality.get(key, 0) for key in ("hardware_verified", "software_estimated"))
        gap = self.database.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(missing_samples), 0) AS missing_samples,
                   COALESCE(SUM(duration_ms), 0) AS duration_ms, COALESCE(MAX(duration_ms), 0) AS longest_ms
            FROM data_gaps WHERE session_id = ?
            """,
            (self.session_id,),
        ).fetchone()
        summary = {
            "session_id": self.session_id,
            "status": status,
            "trial_counts": counts,
            "timing": {
                "invalid_trials": sum(counts.get(key, 0) for key in ("invalid", "aborted")),
                "frame_mismatch_trials": mismatches,
                "dropped_frames_estimate": max(actual - planned, 0),
                "shortfall_frames": max(planned - actual, 0),
                "reasons": invalid_reasons,
            },
            "attention": attention_summary,
            "device_error_count": int(device_errors),
            "alignment": {
                "quality_counts": alignment_quality,
                "aligned_event_rate": aligned / alignment_total if alignment_total else None,
            },
            "data_continuity": dict(gap),
        }
        (self.session_dir / "qc_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report = [
            "# Session QC Report",
            "",
            f"- Session: `{self.session_id}`",
            f"- Status: `{status}`",
            f"- Frame mismatch trials: {mismatches}",
            f"- Estimated dropped frames: {summary['timing']['dropped_frames_estimate']}",
            f"- Alignment coverage: {summary['alignment']['aligned_event_rate']}",
            f"- Data gaps: {gap['count']}",
            f"- Missing samples: {gap['missing_samples']}",
            f"- Attention accuracy: {attention_summary['accuracy']}",
            f"- Attention timeout rate: {attention_summary['timeout_rate']}",
            "",
        ]
        (self.session_dir / "qc_report.md").write_text("\n".join(report), encoding="utf-8")

    def _export_events_tsv(self) -> None:
        eeg_dir = self.session_dir / "eeg"
        eeg_dir.mkdir(exist_ok=True)
        output = eeg_dir / f"{self.participant_id}_{self.session_id}_task-things_events.tsv"
        # A legacy database may lack sync indexes. Avoid an O(events * syncs)
        # LEFT JOIN even there, and preserve duplicate sync rows for auditing.
        syncs: dict[int, list[dict]] = {}
        for sync in self.database.execute(
            "SELECT * FROM eeg_sync WHERE event_id IN "
            "(SELECT event_id FROM events WHERE session_id = ?)", (self.session_id,)
        ):
            syncs.setdefault(sync["event_id"], []).append(dict(sync))
        event_rows = self.database.execute(
            """
            SELECT e.*, t.image_id, t.repetition_index
            FROM events e
            LEFT JOIN trials t ON t.trial_id = e.trial_id
            WHERE e.session_id = ? ORDER BY e.event_seq
            """,
            (self.session_id,),
        )
        empty_sync = dict.fromkeys(("eeg_sample_index", "eeg_timestamp_ns", "alignment_method",
                                   "sync_quality", "latency_ns", "failure_reason"))
        rows = (
            {**dict(event), **sync}
            for event in event_rows
            for sync in syncs.get(event["event_id"], [empty_sync])
        )
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(
                [
                    "event_seq", "event_type", "trial_id", "image_id", "repetition_index",
                    "display_timestamp_ns", "frame_index", "trigger_code", "eeg_sample_index",
                    "eeg_timestamp_ns", "alignment_method", "sync_quality", "latency_ns",
                    "failure_reason", "metadata_json", "object_id", "object_name", "semantic_label", "scene", "view", "category_name",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["event_seq"],
                        row["event_type"],
                        row["trial_id"],
                        row["image_id"],
                        row["repetition_index"],
                        row["timestamp_ns"],
                        row["frame_index"],
                        row["trigger_code"],
                        row["eeg_sample_index"],
                        row["eeg_timestamp_ns"],
                        row["alignment_method"],
                        row["sync_quality"],
                        row["latency_ns"],
                        row["failure_reason"],
                        row["metadata_json"],
                        *[json.loads(row["metadata_json"] or "{}").get(key, "") for key in
                          ("object_id", "object_name", "semantic_label", "scene", "view", "category_name")],
                    ]
                )

    def _export_progress(self) -> None:
        rows = self.database.execute(
            """
            SELECT image_id, valid_repetition_count, updated_at FROM stimulus_progress
            WHERE participant_id = ? ORDER BY image_id
            """,
            (self.participant_id,),
        )
        with (self.session_dir / "progress.tsv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["participant_id", "image_id", "valid_repetition_count", "updated_at"])
            for row in rows:
                writer.writerow(
                    [self.participant_id, row["image_id"], row["valid_repetition_count"], row["updated_at"]]
                )

    def _write_session_metadata(self, status: str, ended_at: str) -> None:
        session_path = self.session_dir / "session.json"
        session = json.loads(session_path.read_text(encoding="utf-8"))
        recording = self.database.execute(
            "SELECT * FROM eeg_recordings WHERE session_id = ?", (self.session_id,)
        ).fetchone()
        session.update({"status": status, "ended_at": ended_at})
        if recording:
            session["recording_id"] = recording["recording_id"]
            session["edf_path"] = recording["edf_path"]
            session["eeg_path"] = recording["edf_path"]
            session["recording_format"] = "brainco-native-sqlite" if recording["source_transport"] == "brainco-full-stream-native" else "edf"
        session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            f'session_id = "{self.session_id}"',
            f'participant_id = "{self.participant_id}"',
            f'run_mode = "{self.run_mode}"',
            f'status = "{status}"',
            f'config_hash = "{self.config_hash}"',
            f"eeg_available = {'true' if recording else 'false'}",
        ]
        if recording:
            lines.extend(
                [
                    f'device_type = "{recording["device_type"]}"',
                    f'source_transport = "{recording["source_transport"]}"',
                    f'edf_path = "{recording["edf_path"]}"',
                    f'sampling_rate_hz = {recording["sampling_rate_hz"]}',
                    f'channel_count = {recording["channel_count"]}',
                    f'sample_count = {recording["sample_count"]}',
                    f'reference = "{recording["reference"] or ""}"',
                ]
            )
        (self.session_dir / "metadata.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_checksums(self) -> None:
        entries: list[str] = []
        for path in sorted(self.session_dir.rglob("*")):
            if path.is_file() and path.name != "checksums.sha256":
                digest = sha256_file(path)
                entries.append(f"{digest}  {path.relative_to(self.session_dir).as_posix()}")
        (self.session_dir / "checksums.sha256").write_text("\n".join(entries) + "\n", encoding="utf-8")


def recover_running_sessions(database: sqlite3.Connection) -> None:
    with database:
        running = list(database.execute("SELECT session_id FROM sessions WHERE status = 'running'"))
        for row in running:
            session_id = row["session_id"]
            event_seq = database.execute(
                "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            database.execute(
                """
                INSERT INTO events(event_seq, session_id, trial_id, event_type, timestamp_ns,
                                   frame_index, trigger_code, metadata_json)
                VALUES (?, ?, NULL, 'RECOVERY_ABORT', ?, NULL, NULL, ?)
                """,
                (
                    event_seq,
                    session_id,
                    time.monotonic_ns(),
                    json.dumps({"reason": "process_interrupted", "recovered": True}),
                ),
            )
        database.execute(
            """
            UPDATE trials SET status = 'aborted', validity = 'aborted', invalid_reason = 'recovered_after_interruption'
            WHERE session_id IN (SELECT session_id FROM sessions WHERE status = 'running')
              AND status IN ('planned', 'presented')
            """
        )
        database.execute(
            "UPDATE sessions SET status = 'incomplete', end_time = ? WHERE status = 'running'",
            (utc_now(),),
        )
