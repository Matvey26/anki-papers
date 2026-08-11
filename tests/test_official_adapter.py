from __future__ import annotations

from types import SimpleNamespace

from anki_papers_sync_worker.official import OfficialAnkiAdapter


class RedirectingCollection:
    def __init__(self) -> None:
        self.output = SimpleNamespace(
            required=3,
            NO_CHANGES=0,
            new_endpoint="https://sync6.ankiweb.net/",
        )
        self.download_endpoint = None
        self.reopened = False

    def sync_collection(self, auth, *, sync_media: bool):
        assert sync_media is False
        return self.output

    def close_for_full_sync(self) -> None:
        pass

    def full_upload_or_download(self, *, auth, server_usn, upload: bool) -> None:
        assert server_usn is None
        assert upload is False
        self.download_endpoint = auth.endpoint

    def reopen(self, *, after_full_sync: bool) -> None:
        self.reopened = after_full_sync


def test_full_download_uses_endpoint_returned_by_ankiweb() -> None:
    collection = RedirectingCollection()
    auth = SimpleNamespace(endpoint=None)

    downloaded = OfficialAnkiAdapter()._normal_or_download(collection, auth)

    assert downloaded is True
    assert auth.endpoint == "https://sync6.ankiweb.net/"
    assert collection.download_endpoint == "https://sync6.ankiweb.net/"
    assert collection.reopened is True
