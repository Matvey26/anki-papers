"""storage / files."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def remove_managed_files(data_dir: Path, paths: list[str | None]) -> None:
    root = data_dir.resolve()
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        if path == root or root not in path.parents:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        try:
            path.parent.rmdir()
        except OSError:
            pass


def replace_managed_file(destination: Path, content: bytes) -> None:
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}-",
        suffix=destination.suffix,
        dir=destination.parent,
        delete=False,
    )
    temporary = Path(temporary_handle.name)
    try:
        with temporary_handle:
            temporary_handle.write(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
