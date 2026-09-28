from __future__ import annotations

from pathlib import Path

from support_webapp import identify, make_app

import articles_to_anki.cards.text as ref_cards_text
import articles_to_anki.quick_dictionary as ref_quick_dictionary
import articles_to_anki.web.routes.documents as ref_web_routes_documents


def test_quick_translation_returns_part_of_speech_groups(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    identify(client)
    monkeypatch.setattr(
        ref_web_routes_documents, "quick_translation_groups",
        lambda word: [{"part_of_speech": "гл.", "translations": [word, "мчаться"]}],
    )

    response = client.get("/api/quick-translation?word=run")

    assert response.status_code == 200
    assert response.json == {
        "groups": [{"part_of_speech": "гл.", "translations": ["run", "мчаться"]}]
    }
    assert client.get("/api/quick-translation?word=two%20words").status_code == 200
    assert client.get("/api/quick-translation?word=one%20two%20three").status_code == 200
    assert client.get("/api/quick-translation?word=is%20of%20high%20quality").status_code == 200
    assert client.get("/api/quick-translation?word=one%20two%20three%20four%20five").status_code == 400


def test_quick_translation_uses_dictionary_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = make_app(tmp_path)
    app.extensions["quick_translation_dictionary"] = ref_quick_dictionary.StarDictDictionary(
        tmp_path / "unused.dict", {"run": [(0, 1)]}
    )
    original_lookup = ref_quick_dictionary.StarDictDictionary.lookup

    def fake_lookup(self, word, *, limit=6):
        if word == "run":
            return [
                {"part_of_speech": "гл.", "translations": ["бежать", "мчаться"]}
            ]
        return []

    monkeypatch.setattr(ref_quick_dictionary.StarDictDictionary, "lookup", fake_lookup)
    client = app.test_client()
    identify(client)

    assert client.get("/api/quick-translation?word=run").json == {
        "groups": [{"part_of_speech": "гл.", "translations": ["бежать", "мчаться"]}]
    }

    assert client.get("/api/quick-translation?word=is%20of%20high%20quality").json == {
        "groups": []
    }
    assert client.get("/api/quick-translation?word=went").json == {"groups": []}
    assert original_lookup is not None


def test_selected_text_normalizes_line_break_hyphens_and_keeps_regular_hyphens() -> None:
    assert ref_cards_text.normalize_selected_text("inter-\nnational") == "international"
    assert ref_cards_text.normalize_selected_text("well-known") == "well-known"
    assert ref_cards_text.is_selectable_target("rule out")
    assert ref_cards_text.is_selectable_target("one two three")
    assert ref_cards_text.is_selectable_target("is of high quality")
    assert not ref_cards_text.is_selectable_target("one two three four five")
