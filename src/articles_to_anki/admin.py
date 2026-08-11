from __future__ import annotations

import argparse
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .security import claim_token_digest
from .webapp import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Administer Anki Papers accounts.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Application data directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    claim = subparsers.add_parser("issue-claim", help="Issue one-time claim code.")
    claim.add_argument("username")
    claim.add_argument("--hours", type=int, default=24)
    args = parser.parse_args(argv)

    app = create_app(
        {
            "DATA_DIR": args.data_dir.resolve(),
            "DATABASE": (args.data_dir / "app.sqlite3").resolve(),
            "AUTO_PROCESS_UPLOADS": False,
        }
    )
    if args.command == "issue-claim":
        token = secrets.token_urlsafe(24)
        with sqlite3.connect(app.config["DATABASE"]) as database:
            user = database.execute(
                "SELECT id, password_hash FROM users WHERE username = ? COLLATE NOCASE",
                (args.username.strip(),),
            ).fetchone()
            if user is None:
                raise SystemExit("Unknown user")
            if user[1]:
                raise SystemExit("Account already has a password")
            timestamp = datetime.now(UTC)
            database.execute(
                "DELETE FROM account_claim_tokens WHERE user_id = ? AND used_at IS NULL",
                (user[0],),
            )
            database.execute(
                """INSERT INTO account_claim_tokens
                   (id, user_id, token_hash, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    secrets.token_hex(16),
                    user[0],
                    claim_token_digest(token),
                    (timestamp + timedelta(hours=max(args.hours, 1))).isoformat(),
                    timestamp.isoformat(),
                ),
            )
        print(token)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
