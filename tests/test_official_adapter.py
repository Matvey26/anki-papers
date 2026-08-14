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


def test_semantic_note_uses_separate_type_and_can_be_refreshed(tmp_path: Path) -> None:
    collection = Collection(str(tmp_path / "collection.anki2"))
    card = {
        "id": "sense-1", "semantic": True, "lemma": "overcome",
        "part_of_speech": "verb", "sense_definition_en": "deal successfully with a difficulty",
        "translations": ["преодолеть"], "contexts": [
            {"id": "a", "source": "user_pdf", "target": "overcame", "sentence": "She overcame the limitation.", "replacement": "преодолела"},
            {"id": "b", "source": "llm_generated", "target": "overcome", "sentence": "Teams must overcome hidden assumptions.", "replacement": "преодолеть"},
        ],
    }
    note = OfficialAnkiAdapter._add_note(collection, card, "meaning", 1)
    assert note.note_type()["name"] == "Anki Papers Semantic"
    assert "sessionStorage" in note.fields[0]
    assert "overcame" in note.fields[0]
    assert (
        'data-contexts="' in note.fields[0]
        and '>She <b>overcame</b> the limitation.<br><small>' in note.fields[0]
    )
    card["contexts"].append(
        {"id": "c", "source": "llm_generated", "target": "Overcoming", "sentence": "Overcoming noise required repeated trials.", "replacement": "преодоление"}
    )
    OfficialAnkiAdapter._update_semantic_note(collection, note, card, "meaning")
    assert "Overcoming" in note.fields[0]
    collection.close()
