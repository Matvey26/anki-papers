from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from anki.collection import Collection
from anki.decks import DeckId
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


def test_managed_notetype_does_not_depend_on_english_basic_name(tmp_path: Path) -> None:
    collection = Collection(str(tmp_path / "collection.anki2"))
    basic = collection.models.by_name("Basic")
    assert basic is not None
    basic["name"] = "Базовая"
    collection.models.update_dict(basic)
    card = {
        "id": "context-1",
        "target": "robust",
        "sentence": "A robust result.",
        "replacement": "надёжный",
        "translations": ["надёжный"],
        "alternatives": ["strong"],
        "document_name": "paper.pdf",
        "page": 1,
    }

    note = OfficialAnkiAdapter._add_note(collection, card, "meaning", 1)

    assert collection.models.by_name("Basic") is None
    assert collection.models.by_name("Anki Papers") is not None
    assert note.fields == ["A <b>robust</b> result.", "• надёжный"]
    collection.close()


def test_reconciled_legacy_note_gets_stable_sync_tags(tmp_path: Path) -> None:
    collection = Collection(str(tmp_path / "collection.anki2"))
    legacy = collection.new_note(collection.models.by_name("Basic"))
    legacy["Front"] = "Old front"
    legacy["Back"] = "Old back"
    legacy.tags = ["article::old", "card::meaning"]
    collection.add_note(legacy, DeckId(1))

    OfficialAnkiAdapter._ensure_sync_tags(collection, legacy, "site-card-1", "meaning")
    OfficialAnkiAdapter._ensure_sync_tags(collection, legacy, "site-card-1", "meaning")

    reloaded = collection.get_note(legacy.id)
    assert reloaded.tags == [
        "anki_papers::site-card-1",
        "article::old",
        "card::meaning",
        "direction::meaning",
    ]
    collection.close()
