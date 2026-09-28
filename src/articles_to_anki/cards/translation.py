"""cards / translation."""
from __future__ import annotations

from typing import Any

from flask import current_app

from articles_to_anki.quick_dictionary import StarDictDictionary


def quick_translation_groups(word: str) -> list[dict[str, Any]]:
    dictionary = current_app.extensions.get("quick_translation_dictionary")
    if isinstance(dictionary, StarDictDictionary):
        return dictionary.lookup(word)
    return []
