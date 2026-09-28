"""extraction / constants."""
from __future__ import annotations

import re

from nltk.stem import PorterStemmer

RECALL_PLACEHOLDER = "[[[TARGET_RU]]]"


_ABBREVIATIONS = {
    "al.",
    "approx.",
    "dr.",
    "e.",
    "e.g.",
    "eq.",
    "eqs.",
    "etc.",
    "fig.",
    "figs.",
    "g.",
    "i.e.",
    "mr.",
    "mrs.",
    "ms.",
    "no.",
    "prof.",
    "sec.",
    "secs.",
    "st.",
    "s.",
    "v.",
    "vs.",
    "v.s.",
}


_LEADING_PUNCTUATION = "\"'“‘([{"


_TRAILING_PUNCTUATION = "\"'”’)]},;:.!?"


_FOOTNOTE_URL_RE = re.compile(r"^\s*\d*\s*(?:https?://|www\.)\S+\s*$", re.IGNORECASE)


_MAX_CONTEXT_CHARS = 420


_VARIANT_FUZZY_SCORE_CUTOFF = 78.0


_VARIANT_MAX_LENGTH_DIFF = 4


_VARIANT_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


_PORTER = PorterStemmer()
