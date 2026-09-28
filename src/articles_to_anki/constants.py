"""web / constants."""
from __future__ import annotations

import logging
import re

USERNAME_RE = re.compile(r"^[\w.\-]{3,32}$", re.UNICODE)


WORD_RE = re.compile(r"^[\w]+(?:['’\-][\w]+)*$", re.UNICODE)


HYPHENATED_LINE_BREAK_RE = re.compile(r"[-\u2010-\u2015\u2212\u00ad]\s*\r?\n\s*")


MAX_TARGET_WORDS = 4


MAX_CLUSTER_CANDIDATES = 5


MAX_CLUSTER_EXAMPLES = 5


CLUSTER_FUZZY_SCORE_CUTOFF = 45.0


ARTICLE_CONTEXT_LIMIT = 4


CONTEXT_CANDIDATES_PER_DOCUMENT = 6


CONTEXT_APPROVAL_CANDIDATE_LIMIT = 14


MAX_CONTEXT_APPROVAL_EXAMPLES = 5


LOGGER = logging.getLogger(__name__)


_ARTICLE_STOPWORDS = {
    "about", "after", "also", "among", "and", "are", "because", "been", "before",
    "being", "between", "both", "but", "can", "could", "does", "during", "each",
    "for", "from", "had", "has", "have", "into", "its", "may", "more", "most",
    "not", "only", "other", "our", "over", "same", "such", "than", "that", "the",
    "their", "these", "they", "this", "through", "under", "using", "was", "were",
    "when", "where", "which", "while", "with", "would",
}
