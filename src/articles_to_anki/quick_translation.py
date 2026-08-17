from __future__ import annotations

from typing import Callable


class MachineTranslator:
    """Local neural machine translation via Argos Translate (English → Russian)."""

    def __init__(self, translate: Callable[[str], str]) -> None:
        self.translate = translate

    @classmethod
    def load(cls) -> MachineTranslator | None:
        try:
            import argostranslate.translate as argos_translate
        except ImportError:
            return None
        try:
            languages = argos_translate.get_installed_languages()
        except Exception:
            return None
        english = next(
            (language for language in languages if language.code == "en"), None
        )
        russian = next(
            (language for language in languages if language.code == "ru"), None
        )
        if english is None or russian is None:
            return None
        translation = english.get_translation(russian)
        if translation is None:
            return None
        return cls(translation.translate)

    def __call__(self, text: str) -> str:
        return self.translate(text).strip()
