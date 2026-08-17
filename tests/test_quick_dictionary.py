from __future__ import annotations

import gzip
import struct
from pathlib import Path

import pytest

from articles_to_anki import quick_dictionary
from articles_to_anki.quick_dictionary import StarDictDictionary, parse_entry


def write_dictionary(directory: Path, entries: list[tuple[str, str]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    index = bytearray()
    data = bytearray()
    for word, definition in entries:
        encoded = definition.encode()
        index.extend(word.encode() + b"\0" + struct.pack(">II", len(data), len(encoded)))
        data.extend(encoded)
    with gzip.open(directory / "eng-rus.idx.gz", "wb") as output:
        output.write(index)
    (directory / "eng-rus.dict").write_bytes(data)


def test_stardict_lookup_groups_parts_of_speech_and_removes_stress(tmp_path: Path) -> None:
    write_dictionary(
        tmp_path,
        [
            (
                "paper",
                '<div><div><font class="grammar">noun</font></div>'
                "A material.<ol><li><div>бума́га</div></li>"
                "<li><div>докуме́нт</div></li></ol></div>",
            ),
            (
                "paper",
                '<div><div><font class="grammar">verb</font></div>'
                "<div>окле́ивать бума́гой</div></div>",
            ),
        ],
    )

    dictionary = StarDictDictionary.load(tmp_path)

    assert dictionary is not None
    assert dictionary.lookup("paper") == [
        {"part_of_speech": "сущ.", "translations": ["бумага", "документ"]},
        {"part_of_speech": "гл.", "translations": ["оклеивать бумагой"]},
    ]


def test_stardict_lookup_matches_casefolded_exact_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quick_dictionary, "lemma_candidates", lambda word: [])
    write_dictionary(
        tmp_path,
        [
            (
                "acquire",
                '<div><div><font class="grammar">verb</font></div>'
                "<div>приобретать</div></div>",
            ),
        ],
    )
    dictionary = StarDictDictionary.load(tmp_path)

    assert dictionary is not None
    assert dictionary.lookup("Acquire")[0]["translations"] == ["приобретать"]
    assert dictionary.lookup("acquired") == []


def test_stardict_lookup_aggregates_translations_across_lemma_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        quick_dictionary,
        "lemma_candidates",
        lambda word: ["acquire", "obtain"],
    )
    write_dictionary(
        tmp_path,
        [
            (
                "acquire",
                '<div><div><font class="grammar">verb</font></div>'
                "<div>приобретать</div><div>добывать</div></div>",
            ),
            (
                "obtain",
                '<div><div><font class="grammar">verb</font></div>'
                "<div>добывать</div><div>получать</div></div>",
            ),
        ],
    )
    dictionary = StarDictDictionary.load(tmp_path)

    assert dictionary is not None
    assert dictionary.lookup("acquired") == [
        {"part_of_speech": "гл.", "translations": ["приобретать", "добывать", "получать"]}
    ]


def test_wordnet_lemma_candidates_cover_inflected_forms() -> None:
    pytest.importorskip("nltk")
    candidates = quick_dictionary.lemma_candidates("went")
    if not candidates:
        pytest.skip("wordnet corpus is not installed")
    assert "go" in candidates
    assert "child" in quick_dictionary.lemma_candidates("children")
    assert "write" in quick_dictionary.lemma_candidates("written")


def test_parse_entry_cleans_wiktionary_links_without_losing_short_i() -> None:
    assert parse_entry(
        '<div><font class="grammar">adjective</font></div>'
        "<div>[[бума́жный]]</div><div>[[скорый|ско́рый]]</div>"
    ) == ("прил.", ["бумажный", "скорый"])