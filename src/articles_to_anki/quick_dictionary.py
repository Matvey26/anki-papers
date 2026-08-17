from __future__ import annotations

import gzip
import html
import os
import re
import struct
from collections import defaultdict
from pathlib import Path


_LEMMATIZER = None


def lemma_candidates(word: str) -> list[str]:
    """All WordNet base forms for a word across parts of speech."""
    global _LEMMATIZER
    if _LEMMATIZER is None:
        try:
            import nltk
            from nltk.corpus import wordnet as wn

            nltk_data = os.environ.get("ANKI_PAPERS_DATA_DIR")
            if nltk_data:
                nltk.data.path.insert(0, str(Path(nltk_data) / "nltk_data"))
            nltk.data.path.insert(0, str(Path.cwd() / "data" / "nltk_data"))
            _LEMMATIZER = wn
        except (ImportError, LookupError, OSError):
            _LEMMATIZER = None
    if _LEMMATIZER is None:
        return []

    word = word.strip(" -'")
    if not word:
        return []
    candidates: list[str] = []
    for pos in ("n", "v", "a", "r"):
        try:
            morphy = _LEMMATIZER._morphy(word, pos, True)
        except LookupError:
            return []
        for lemma in morphy:
            if lemma != word and lemma not in candidates:
                candidates.append(lemma)
    return candidates


_GRAMMAR_RE = re.compile(
    r'<font\s+[^>]*class=["\']grammar["\'][^>]*>([^<]+)</font>', re.IGNORECASE
)
_LEAF_DIV_RE = re.compile(r"<div>([^<>]*)</div>", re.IGNORECASE)
_WIKI_LINK_RE = re.compile(r"\[\[(?:[^]|]+\|)?([^]]+)]]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

PARTS_OF_SPEECH = {
    "noun": "сущ.",
    "verb": "гл.",
    "adjective": "прил.",
    "adverb": "нареч.",
    "pronoun": "мест.",
    "preposition": "предл.",
    "conjunction": "союз",
    "interjection": "межд.",
    "numeral": "числ.",
    "article": "артикль",
    "determiner": "опред.",
    "proper noun": "имя собств.",
}


class StarDictDictionary:
    """Small read-only StarDict reader for local quick translation."""

    def __init__(
        self,
        data_path: Path,
        entries: dict[str, list[tuple[int, int]]],
    ) -> None:
        self.data_path = data_path
        self.entries = entries

    @classmethod
    def load(cls, directory: Path) -> StarDictDictionary | None:
        index_path = directory / "eng-rus.idx.gz"
        data_path = directory / "eng-rus.dict"
        if not index_path.is_file() or not data_path.is_file():
            return None

        raw_index = gzip.open(index_path, "rb").read()
        entries: dict[str, list[tuple[int, int]]] = defaultdict(list)
        position = 0
        while position < len(raw_index):
            word_end = raw_index.find(b"\0", position)
            if word_end < 0 or word_end + 9 > len(raw_index):
                raise ValueError("Invalid StarDict index")
            word = raw_index[position:word_end].decode("utf-8").casefold()
            offset, size = struct.unpack(">II", raw_index[word_end + 1 : word_end + 9])
            entries[word].append((offset, size))
            position = word_end + 9
        return cls(data_path, dict(entries))

    def lookup(self, query: str, *, limit: int = 6) -> list[dict[str, object]]:
        key = query.casefold().strip(" -'")
        keys = [key, *lemma_candidates(key)]
        if not keys:
            return []

        groups: dict[str, list[str]] = {}
        with self.data_path.open("rb") as dictionary:
            for key in keys:
                offsets = self.entries.get(key)
                if not offsets:
                    continue
                for offset, size in offsets:
                    dictionary.seek(offset)
                    part_of_speech, translations = parse_entry(
                        dictionary.read(size).decode("utf-8")
                    )
                    if not translations:
                        continue
                    values = groups.setdefault(part_of_speech, [])
                    seen = {value.casefold() for value in values}
                    for translation in translations:
                        if translation.casefold() not in seen:
                            values.append(translation)
                            seen.add(translation.casefold())
                        if len(values) == limit:
                            break
        return [
            {"part_of_speech": part_of_speech, "translations": translations}
            for part_of_speech, translations in groups.items()
        ]


def parse_entry(entry: str) -> tuple[str, list[str]]:
    grammar = _GRAMMAR_RE.search(entry)
    raw_part = html.unescape(grammar.group(1)).strip().casefold() if grammar else ""
    part_of_speech = PARTS_OF_SPEECH.get(raw_part, raw_part)
    translations: list[str] = []
    seen: set[str] = set()
    for raw_value in _LEAF_DIV_RE.findall(entry):
        value = _WIKI_LINK_RE.sub(r"\1", html.unescape(raw_value))
        value = value.replace("\u0301", "")
        value = " ".join(value.split()).strip(" ,;")
        key = value.casefold()
        if value and _CYRILLIC_RE.search(value) and key not in seen:
            translations.append(value)
            seen.add(key)
    return part_of_speech, translations
