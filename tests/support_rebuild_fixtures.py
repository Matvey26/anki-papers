from __future__ import annotations

from typing import Any


def _rebuild_download(client) -> tuple[dict, Any]:
    status = client.get("/api/rebuild/status")
    assert status.status_code == 200
    payload = status.get_json()["rebuild_job"]
    assert payload is not None
    assert payload["state"] == "succeeded"
    assert payload["progress"] == 100
    assert payload["download_url"]
    download = client.get(payload["download_url"])
    assert download.status_code == 200
    assert download.headers["Content-Disposition"].startswith(
        "attachment; filename=anki-papers-rebuild-"
    )
    import re

    stamp = re.search(
        r"filename=anki-papers-rebuild-(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.apkg",
        download.headers["Content-Disposition"],
    )
    assert stamp, "download filename must carry the build timestamp"
    assert payload["created_at"].startswith(stamp.group(1)[:10] + "T")
    return payload, download
