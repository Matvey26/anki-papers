"""web / routes / health."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask import Flask, Response, jsonify, redirect, session, url_for

from articles_to_anki.storage.database import get_database


def register(app: Flask) -> None:
    @app.get("/health")
    def health() -> Response:
        return jsonify(ok=True)

    @app.get("/health/worker")
    def worker_health() -> Response:
        row = get_database().execute(
            "SELECT MAX(updated_at) AS updated_at FROM worker_heartbeat"
        ).fetchone()
        if row is None or not row["updated_at"]:
            return jsonify(ok=False), 503
        try:
            updated = datetime.fromisoformat(row["updated_at"])
        except ValueError:
            return jsonify(ok=False), 503
        healthy = datetime.now(UTC) - updated <= timedelta(seconds=90)
        return jsonify(ok=healthy), 200 if healthy else 503

    @app.get("/")
    def index() -> Response:
        if "user_id" not in session:
            return redirect(url_for("login"))
        return redirect(url_for("dashboard"))
