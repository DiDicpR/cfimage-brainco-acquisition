"""Read-only paired previews with the existing saved EEG verification."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from .collection import saved_progress
from .positions import read_catalog
from .paired import paired_summary


def preview_paired(experiment, participant, mode):
    stimuli, metadata = read_catalog(Path(experiment['paths']['stimulus_manifest']).resolve())
    database = None
    try:
        path = Path(experiment['paths']['database']).resolve()
        if path.is_file():
            database = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            database.row_factory = sqlite3.Row
        progress, rejected = saved_progress(database, participant, mode,
            Path(experiment['paths']['dataset_root']).resolve(), by_target_side=True)
        summary = paired_summary(stimuli, metadata, progress, experiment)
        previous = database.execute(
            'SELECT session_id,status FROM sessions WHERE participant_id=? AND run_mode=? '
            'ORDER BY start_time DESC LIMIT 1', (participant, mode)).fetchone() if database else None
        summary.update(participant_id=participant, run_mode=mode,
            unverified_trials=rejected, previous_session=dict(previous) if previous else None)
        return summary
    finally:
        if database is not None: database.close()
