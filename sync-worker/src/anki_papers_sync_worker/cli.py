from __future__ import annotations

import argparse
import os
from pathlib import Path

from .worker import SyncWorker, run_forever


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Anki Papers sync worker.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("ANKI_PAPERS_DATA_DIR", "data")),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve()
    worker = SyncWorker(data_dir / "app.sqlite3", data_dir)
    worker.startup_cleanup()
    if args.once:
        worker.run_once()
        return 0
    run_forever(worker, max(args.poll_seconds, 0.25))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
