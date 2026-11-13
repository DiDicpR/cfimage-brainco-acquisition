from __future__ import annotations

import sqlite3

from .database import SCHEMA


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()
