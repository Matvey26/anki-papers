"""rebuilding / constants."""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

LOGGER = logging.getLogger(__name__)


REBUILD_REVISION = "v1"


REBUILD_DECK_NAME = "Anki Papers (пересборка)"


_COLLECTION_NAMES = ("collection.anki21b", "collection.anki21", "collection.anki2")


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


_TEMPLATE_JSON = Path(__file__).parents[1] / "rebuild_template.json"


_SCHEDULE_FIELDS = (
    "type",
    "queue",
    "due",
    "ivl",
    "factor",
    "reps",
    "lapses",
    "left",
    "odue",
    "odid",
)


ProgressReporter: type = Callable[[int, str], None]


_DECK_REPLAY_END = 85
