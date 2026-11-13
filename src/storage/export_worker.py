from __future__ import annotations

import sqlite3
import traceback
from pathlib import Path

from PySide6.QtCore import QThread

from .database import SessionStore


class SessionExportWorker(QThread):
    """Only used after recording and trial writes have stopped.

    SQLite connections are thread-owned. Never move the acquisition connection
    into the worker, and never let an export exception escape a Qt callback.
    """

    def __init__(self, database_path: Path, store: SessionStore, parent=None) -> None:
        super().__init__(parent)
        self.database_path = database_path
        self.session_dir = store.session_dir
        self.options = dict(
            dataset_root=store.dataset_root, participant_id=store.participant_id,
            session_id=store.session_id, day_index=store.day_index,
            config_hash=store.config_hash, random_seed=store.random_seed,
            run_mode=store.run_mode, locked_config=store.locked_config,
            manifest_path=store.manifest_path,
        )
        self.error: str | None = None

    def run(self) -> None:
        connection = None
        try:
            connection = sqlite3.connect(str(self.database_path), timeout=30)
            connection.row_factory = sqlite3.Row
            store=SessionStore(connection, **self.options)
            store.export_finalized()
            from ..session_handoff import export_handoff
            export_handoff(store)
        except Exception:
            self.error = traceback.format_exc()
            try:
                (self.session_dir / "export_error.log").write_text(self.error, encoding="utf-8")
            except OSError:
                pass
        finally:
            if connection is not None:
                connection.close()
