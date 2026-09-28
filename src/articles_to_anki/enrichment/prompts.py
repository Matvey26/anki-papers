"""enrichment / prompts."""
from __future__ import annotations

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


DEEPSEEK_MODEL_PREFIX = "deepseek/deepseek-v4-flash-0731"


DEFAULT_MODEL = f"{DEEPSEEK_MODEL_PREFIX}:nitro"


DEFAULT_SEMANTIC_MODEL = DEFAULT_MODEL


CACHE_VERSION = "v5"


MAX_APPROVAL_ATTEMPTS = 3


RETRY_TEMPERATURES = (0.2, 0.1, 0.3, 0.0, 0.25)


RETRY_INSTRUCTIONS = (
    "Retry independently. Re-read the sentence before choosing the target's exact sense.",
    "Start over with different wording. Draft each field mentally, then emit only valid JSON.",
    "Prioritize schema validity and exact property names; use conservative common translations.",
    "Final attempt: solve one item at a time, verify every constraint, then return the schema.",
)


SYSTEM_PROMPT = """\
You create English-to-Russian vocabulary cards for an advanced English learner.
For every input item:
1. Preserve its id exactly.
2. First disambiguate the highlighted target from the FULL sentence. In
   context_explanation_ru, briefly explain in Russian which exact sense applies and name the
   nearby clue, collocation, or grammatical construction that establishes it.
3. Start translations_ru with the single best Russian answer for this occurrence. Add up to four
   UNIQUE close variants only when each deserves the same score as a learner's answer in this
   exact sentence. A related concept that merely preserves the sentence's general message is not
   the same lexical meaning. Reject hypernyms, hyponyms, prerequisites, consequences, paraphrases,
   and words with weaker or stronger commitment even when the full sentence remains plausible.
   Quality is more important than quantity: one precise translation is a complete answer. Never
   add a weaker translation or another dictionary sense to fill a list.
4. Give replacement_ru in the exact Russian grammatical form that can replace the English
   target inside the otherwise English sentence. Preserve tense, number, and discourse role.
   Mentally substitute it into the full sentence and verify that the sentence's intended
   meaning remains intact. Do not add a comma, period, colon, semicolon, exclamation mark, or
   question mark after replacement_ru: punctuation adjacent to the target is already preserved.
5. forbidden_alternatives_en is NOT a thesaurus or association list. Start it as an empty list.
   Consider at most 6 simpler English near-synonyms that a learner might use as the card answer.
   Add a candidate only after literally replacing the exact target span with it while keeping
   every other character of the sentence unchanged. The resulting sentence must be natural and
   grammatical, preserve the target's syntactic role and collocation, and state substantially the
   SAME claim. Reject a candidate if a native editor would need to change any neighboring
   preposition, object, article, agreement, punctuation, or word order. A dictionary synonym is
   invalid when it fits only after such an edit. For fixed constructions and strong collocations,
   an empty list is normal and preferable to approximate alternatives.
   NEVER give antonyms, opposites, unrelated words, Russian words, the target itself, or trivial
   spelling/case variants. Reject candidates that negate, reverse, weaken, strengthen, broaden,
   or narrow the original claim merely because they are topically related.
6. Treat a multiword target as one expression.
7. context_explanation_ru, every item in translations_ru, and replacement_ru MUST be written
   in Russian Cyrillic. Never leave the English target in replacement_ru.
The object for each item MUST contain exactly these five property names: id,
context_explanation_ru, translations_ru, replacement_ru, forbidden_alternatives_en. Never
rename any property. The near-synonym list is ALWAYS stored under the literal property name
forbidden_alternatives_en; never rename it to near_synonyms or anything else.
Return every input id once and only once. Follow the supplied JSON Schema exactly.
"""


CLUSTER_SYSTEM_PROMPT = """\
Assign ONE highlighted English word or expression to a learnable word-formation cluster and build
its English-to-Russian card context.

You receive the raw highlight, its normalized form, its full sentence, and zero to five candidate
clusters found only by fuzzy spelling similarity. Each candidate has a stable cluster_id, a leader,
and up to five real highlights with their contexts. Fuzzy similarity is retrieval only: it is not
evidence that meanings or word families match.

Set cluster_id to exactly one supplied candidate cluster_id only when the new highlight is
word-formation-related to that cluster AND shares its central learnable meaning. Inflections and
derivations may share a cluster: acquire/acquires/acquired/acquisition can use leader acquire.
Homonyms with identical spelling but unrelated meanings must remain separate clusters: train as a
noun meaning a railway vehicle and train as a verb meaning practise need different cluster_id
values. Different senses that would teach different answers also need separate clusters.

Set cluster_id to the literal value new_cluster when no candidate qualifies. This is always a valid
choice, even when candidates exist. Never invent a cluster_id. For new_cluster, leader must be the
canonical lowercase representative: a base word, normalized phrase, or fixed expression. For an
existing cluster, repeat its supplied leader exactly. Fixed expressions that cannot be usefully
re-formed remain singleton clusters, e.g. it's worth has leader it's worth.

cluster_definition_en must precisely cover the selected cluster after adding this occurrence. For a
new cluster it defines this occurrence's sense. For an existing cluster it is a concise umbrella
definition that covers both old examples and the new occurrence without admitting unrelated senses.

Give 1-4 precise Russian translations of the highlighted span. replacement_ru must translate only
that span, preserving its contextual case, number, tense, and role without absorbing neighboring
words. Never invent sources or statistics.

Build source_distractors in two stages. First brainstorm:
- substitutes_en: as many defensible words or compact phrases as the schema allows that can replace
  only the highlighted surface while all surrounding text stays frozen. The result must read as a
  natural, meaningful sentence; a meaning shift or slight style mismatch is allowed.
- related_en: as many close semantic neighbors as possible, whether or not exact insertion works.
  Exclude distant associations, antonyms, and words needing major semantic qualifications.
Then judge the visible subsets conservatively:
- valid_substitutes_en: only high-confidence items from substitutes_en that pass literal insertion.
- valid_related_en: only high-confidence items from related_en that are closest in meaning,
  regardless of whether literal insertion works. Overlap between valid lists is allowed.
Before adding any substitute, silently read the full literal result. In a fixed frame or strong
collocation, accept only words established with the same neighbors; reject any candidate needing a
different verb, preposition, determiner, or word order. Never repair the sentence or test a
different construction. Exclude the target and spelling/case variants, Russian words,
explanations, and sentences. Valid lists may be empty; quality beats count.
Return only JSON matching the schema.
"""


CONTEXT_APPROVAL_SYSTEM_PROMPT = """\
You decide which real sentences from the user's articles may be added as extra learning contexts
to an existing English vocabulary card.

The card trains ONE lexical sense of the word family:
- leader: {leader}
- meaning: {definition}
- Russian translations: {translations_ru}
- already known contexts: {known_contexts}

Each candidate sentence contains the surface form in angle brackets, e.g. <impaired>. Surfaces
are found only by spelling/word-formation similarity, NOT by meaning, so any of them may be a
different word, a homonym, a different sense, or a proper noun.

Set suitable=true only when ALL of these hold:
1. The surface in THIS sentence expresses exactly the sense the card trains. A different sense of
   the same spelling, a homonym, a fixed expression with its own meaning, or a proper noun is NOT
   suitable, even inside a topically related sentence.
2. The sentence is natural, self-contained, and understandable without extra context.
3. The sentence would genuinely help an English learner recall this exact meaning.

Return one object with properties id and suitable for EVERY candidate id, exactly once.
"""
