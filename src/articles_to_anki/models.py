from __future__ import annotations

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
        min_length=2,
        max_length=5,
        description=(
            "Two to five distinct Russian translations that all express the target's "
            "meaning in this exact sentence, never unrelated dictionary senses."
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
        min_length=2,
        max_length=6,
        description=(
            "Unique, simpler English near-synonyms that preserve the contextual meaning "
            "but are not the target answer. Never antonyms or unrelated words."
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
    "phrase",
    "other",
]


class SemanticAnalysis(StrictModel):
    """LLM result used to group contexts into one learnable lexical sense."""

    lemma: str = Field(min_length=1, max_length=100)
    family_key: str = Field(min_length=1, max_length=100)
    part_of_speech: SemanticPartOfSpeech
    sense_definition_en: str = Field(min_length=3, max_length=300)
    translations_ru: list[str] = Field(min_length=1, max_length=4)
    replacement_ru: str = Field(min_length=1, max_length=100)
    generated_sentence: str = Field(min_length=12, max_length=500)
    generated_surface: str = Field(min_length=1, max_length=100)
    generated_translation_ru: str = Field(min_length=1, max_length=100)

    @field_validator("lemma", "sense_definition_en", "generated_sentence", "generated_surface")
    @classmethod
    def stripped_english(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("family_key")
    @classmethod
    def normalized_family_key(cls, value: str) -> str:
        cleaned = " ".join(value.casefold().split())
        if not any("a" <= char <= "z" for char in cleaned):
            raise ValueError("family key must contain English letters")
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


class SemanticCandidate(StrictModel):
    id: str
    family_key: str
    lemmas: list[str] = Field(min_length=1, max_length=12)
    parts_of_speech: list[SemanticPartOfSpeech] = Field(min_length=1, max_length=6)
    sense_definition_en: str


class SemanticMatch(StrictModel):
    card_id: str | None
    relationship: Literal["same_sense", "related_sense", "new_card"]
    merged_sense_definition_en: str | None
    rationale_ru: str = Field(
        min_length=1,
        max_length=300,
        pattern=r".*[А-Яа-яЁё].*",
    )

    @model_validator(mode="after")
    def valid_merge_shape(self) -> SemanticMatch:
        if self.relationship == "new_card":
            if self.card_id is not None or self.merged_sense_definition_en is not None:
                raise ValueError("new card must not select or rewrite an existing card")
        elif self.card_id is None or not self.merged_sense_definition_en:
            raise ValueError("semantic merge must select a card and return a definition")
        return self


class SemanticMatchResponse(StrictModel):
    match: SemanticMatch
