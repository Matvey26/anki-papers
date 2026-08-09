from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
