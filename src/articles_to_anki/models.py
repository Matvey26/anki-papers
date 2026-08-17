from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetContext(StrictModel):
    id: str = Field(description="Stable identifier for this target occurrence.")
    target: str = Field(min_length=1, description="Highlighted English word or phrase.")
    sentence: str = Field(min_length=1, description="Source English sentence.")
    sentence_html: str = Field(
        min_length=1,
        description="Sentence with only this target occurrence in bold.",
    )
    recall_template_html: str = Field(
        min_length=1,
        description="Sentence with the target replaced by an internal placeholder.",
    )
    source_page: int = Field(ge=1)
    highlight_coverage: float = Field(ge=0, le=1)


class EnrichmentRequestItem(StrictModel):
    id: str
    target: str
    sentence: str


class EnrichedItem(StrictModel):
    id: str = Field(description="The unchanged identifier from the input.")
    context_explanation_ru: str = Field(
        min_length=1,
        pattern=r".*[А-Яа-яЁё].*",
        description=(
            "Brief Russian explanation of the target's exact meaning in this sentence "
            "and the nearby words or construction that establish that meaning."
        ),
    )
    translations_ru: list[str] = Field(
        min_length=1,
        max_length=5,
        description=(
            "The single best Russian answer followed by at most four equally precise close "
            "variants for this exact occurrence. One precise translation is complete; never "
            "add related concepts, paraphrases, prerequisites, consequences, hypernyms, "
            "hyponyms, or weaker dictionary senses to increase the count."
        ),
    )
    replacement_ru: str = Field(
        min_length=1,
        pattern=r".*[А-Яа-яЁё].*",
        description=(
            "Cyrillic Russian replacement in the exact grammatical form needed inside "
            "the sentence; it must never contain an untranslated English target."
        ),
    )
    forbidden_alternatives_en: list[str] = Field(
        min_length=0,
        max_length=6,
        description=(
            "An exact-span substitution list, not a thesaurus: zero to six unique, simpler "
            "English near-synonyms that produce a natural sentence when only the target span "
            "is replaced and every other character stays unchanged. Empty is preferable to "
            "any candidate requiring a surrounding edit or creating an awkward collocation."
        ),
    )

    @field_validator("translations_ru")
    @classmethod
    def translations_must_be_unique_russian(
        cls, values: list[str]
    ) -> list[str]:
        stripped = [value.strip() for value in values]
        normalized = [value.casefold() for value in stripped]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Russian translations must be unique")
        if any(
            not value or not any(char in "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя" for char in value)
            for value in stripped
        ):
            raise ValueError("Russian translations must contain Cyrillic letters")
        return stripped

    @field_validator("replacement_ru")
    @classmethod
    def replacement_must_not_duplicate_source_punctuation(cls, value: str) -> str:
        stripped = value.strip()
        if stripped.endswith((",", ".", ";", ":", "!", "?")):
            raise ValueError(
                "Russian replacement must not include trailing punctuation; "
                "source punctuation is preserved by the card template"
            )
        return stripped

    @field_validator("forbidden_alternatives_en")
    @classmethod
    def alternatives_must_be_unique_english(
        cls, values: list[str]
    ) -> list[str]:
        normalized = [value.strip().casefold() for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("forbidden alternatives must be unique")
        if any(not value or not any("a" <= char <= "z" for char in value) for value in normalized):
            raise ValueError("forbidden alternatives must contain English letters")
        return [value.strip() for value in values]


class EnrichmentBatch(StrictModel):
    items: list[EnrichedItem]


SemanticPartOfSpeech = Literal[
    "noun",
    "verb",
    "adjective",
    "adverb",
    "preposition",
    "conjunction",
    "pronoun",
    "determiner",
    "particle",
    "phrase",
    "other",
]


class RecallDistractors(StrictModel):
    substitutes_en: list[str] = Field(
        min_length=0,
        max_length=12,
        description=(
            "Broad brainstorm of English words or phrases that can literally replace the "
            "highlighted span while leaving a natural, meaningful sentence. Meaning may "
            "shift and style may be imperfect."
        ),
    )
    related_en: list[str] = Field(
        min_length=0,
        max_length=12,
        description=(
            "Broad brainstorm of the closest English semantic neighbors, regardless of "
            "whether they fit the sentence's exact grammar or collocation."
        ),
    )
    valid_substitutes_en: list[str] = Field(
        min_length=0,
        max_length=4,
        description=(
            "Other English answers that naturally replace exactly the highlighted span, "
            "preserve its grammar/collocation and reasonably fit the contextual meaning, but "
            "are not the target this recall card is testing. Empty is valid."
        ),
    )
    valid_related_en: list[str] = Field(
        min_length=0,
        max_length=4,
        description=(
            "High-confidence English words or phrases closest to the highlighted meaning, "
            "regardless of whether exact insertion fits the sentence. Never include antonyms "
            "or distant associations. Empty is valid."
        ),
    )

    @field_validator(
        "substitutes_en",
        "related_en",
        "valid_substitutes_en",
        "valid_related_en",
    )
    @classmethod
    def unique_english_distractors(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        normalized = [value.casefold() for value in cleaned]
        if len(normalized) != len(set(normalized)):
            raise ValueError("recall distractors must be unique")
        if any(
            not value or not any("a" <= char <= "z" for char in value.casefold())
            for value in cleaned
        ):
            raise ValueError("recall distractors must contain English letters")
        return cleaned

class ClusterExample(StrictModel):
    highlight: str = Field(min_length=1, max_length=100)
    context: str = Field(min_length=1, max_length=1200)


class ClusterCandidate(StrictModel):
    cluster_id: str
    leader: str = Field(min_length=1, max_length=100)
    examples: list[ClusterExample] = Field(min_length=1, max_length=5)


class ClusterAnalysis(StrictModel):
    """One LLM result that assigns a highlight and builds its card context."""

    cluster_id: str = Field(
        description="One supplied cluster_id or the literal new_cluster."
    )
    leader: str = Field(
        min_length=1,
        max_length=100,
        description=(
            "Canonical lowercase representative for a new cluster; repeat the supplied "
            "leader when selecting an existing cluster."
        ),
    )
    part_of_speech: SemanticPartOfSpeech
    cluster_definition_en: str = Field(min_length=3, max_length=300)
    translations_ru: list[str] = Field(min_length=1, max_length=4)
    replacement_ru: str = Field(min_length=1, max_length=100)
    generated_sentence: str = Field(min_length=12, max_length=500)
    generated_surface: str = Field(min_length=1, max_length=100)
    generated_translation_ru: str = Field(
        min_length=1,
        max_length=100,
        description=(
            "A compact Russian replacement for generated_surface only, in the exact "
            "grammatical form required by generated_sentence; never a sentence translation."
        ),
    )
    source_distractors: RecallDistractors
    generated_distractors: RecallDistractors

    @field_validator("cluster_definition_en", "generated_sentence", "generated_surface")
    @classmethod
    def stripped_english(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("leader")
    @classmethod
    def normalized_leader(cls, value: str) -> str:
        cleaned = " ".join(value.casefold().split())
        if not cleaned:
            raise ValueError("leader must not be blank")
        return cleaned

    @field_validator("translations_ru")
    @classmethod
    def russian_translations(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not any("а" <= char.casefold() <= "я" or char.casefold() == "ё" for char in value) for value in cleaned):
            raise ValueError("translations must contain Cyrillic")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("translations must be unique")
        return cleaned

    @field_validator("replacement_ru", "generated_translation_ru")
    @classmethod
    def russian_replacements(cls, value: str) -> str:
        cleaned = value.strip()
        if not any(
            "а" <= char.casefold() <= "я" or char.casefold() == "ё"
            for char in cleaned
        ):
            raise ValueError("replacement must contain Cyrillic")
        return cleaned

    @model_validator(mode="after")
    def replacements_are_compact_phrases(self) -> ClusterAnalysis:
        for field_name, value, english_surface in (
            ("replacement_ru", self.replacement_ru, self.leader),
            (
                "generated_translation_ru",
                self.generated_translation_ru,
                self.generated_surface,
            ),
        ):
            russian_words = re.findall(r"[A-Za-zА-Яа-яЁё0-9-]+", value)
            english_words = re.findall(r"[A-Za-z0-9-]+", english_surface)
            maximum_words = min(8, max(5, len(english_words) * 2 + 1))
            if len(russian_words) > maximum_words:
                raise ValueError(
                    f"{field_name} must translate only the highlighted surface, "
                    "not the full sentence"
                )
            if any(mark in value for mark in ("\n", ".", ";", "!", "?")):
                raise ValueError(f"{field_name} must not contain sentence punctuation")
        return self
