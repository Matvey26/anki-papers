"""Keep owned source files small enough to review as one responsibility."""
from pathlib import Path


def test_owned_source_files_fit_line_budget():
    root = Path(__file__).parents[1]
    oversized = []
    for directory in ("src", "sync-worker/src", "tests", "scripts", "deploy"):
        for path in (root / directory).rglob("*"):
            if path.suffix not in {".py", ".js", ".mjs", ".css", ".html", ".sh"}:
                continue
            if "vendor" in path.parts:
                continue
            count = len(path.read_text().splitlines())
            if count > 400:
                oversized.append(f"{path.relative_to(root)}: {count}")
    assert not oversized, "Split by responsibility:\n" + "\n".join(oversized)
