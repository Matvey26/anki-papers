"""storage / database."""
from __future__ import annotations

import sqlite3

from flask import current_app, g


def get_database() -> sqlite3.Connection:
    if "database" not in g:
        connection = sqlite3.connect(current_app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.database = connection
    return g.database


def close_database(_: BaseException | None = None) -> None:
    connection = g.pop("database", None)
    if connection is not None:
        connection.close()
