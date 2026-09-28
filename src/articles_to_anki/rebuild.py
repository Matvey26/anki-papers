"""rebuild."""
from __future__ import annotations

import io
import sqlite3
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import zstandard

from articles_to_anki.apkg import checksum, plain_text
from articles_to_anki.cards.rendering import semantic_card_rows
from articles_to_anki.rebuilding.collection import (
    _avoid_default_deck_id,
    _connect_collection,
    _empty_collection,
    _open_old_collection,
    _rebuild_note_type_id,
    _rename_deck,
)
from articles_to_anki.rebuilding.constants import (
    _DECK_REPLAY_END,
    _SCHEDULE_FIELDS,
    LOGGER,
    ProgressReporter,
)
from articles_to_anki.rebuilding.identity import (
    _deck_name,
    _rebuild_deck_id,
    _rebuild_guid,
    _stable_note_id,
)
from articles_to_anki.rebuilding.schedules import (
    _extract_schedules,
    _resolve_source_decks,
    _site_direction,
)
from articles_to_anki.rebuilding.semantic import rebuild_semantic_deck


def build_rebuilt_deck_apkg(
    database: sqlite3.Connection,
    user_id: int,
    *,
    data_dir: Path,
    source_path: Path | None = None,
    selected_deck_id: int | None = None,
    progress: ProgressReporter | None = None,
) -> bytes:
    """Rebuild all highlights into a fresh APKG with carried-over schedules.

    The rebuilt deck contains one two-field note per semantic card (meaning
    and recall rows), tagged with the same `anki_papers::<card id>` site tags
    as the live deck. When an old collection is available (AnkiWeb mirror of
    the chosen deck), card states from matching old notes are copied across so
    learning progress survives; otherwise new cards start fresh. The stored
    data is never modified.
    """
    if progress is None:
        progress = lambda _percent, _stage: None
    progress(1, "Начинаю сборку")
    replay = rebuild_semantic_deck(
        database,
        user_id,
        data_dir=data_dir,
        progress=lambda percent, stage: progress(percent * _DECK_REPLAY_END // 100, stage),
    )
    cards = replay["cards"]
    if not cards:
        raise RuntimeError("Нет сохранённых слов для пересобранной колоды")
    rows: list[dict[str, str]] = []
    for card in cards:
        try:
            rows.extend(semantic_card_rows(card))
        except (KeyError, TypeError, ValueError):
            LOGGER.warning("Rebuild: skipping card %s without usable contexts", card["id"])
    if not rows:
        raise RuntimeError("Нет карточек для пересобранной колоды")
    progress(88, "Собираю APKG")

    now_seconds = int(time.time())
    deck_name = _deck_name(datetime.fromtimestamp(now_seconds, tz=UTC))
    with tempfile.TemporaryDirectory(prefix="anki-papers-rebuild-") as temporary_name:
        temporary = Path(temporary_name)
        collection_path, is_compressed = _open_old_collection(source_path, temporary)
        connection = _connect_collection(collection_path)
        try:
            schedules = _extract_schedules(
                connection,
                deck_ids=(
                    _resolve_source_decks(connection, selected_deck_id)
                    if selected_deck_id is not None
                    else None
                ),
            )
            deck_id = _avoid_default_deck_id(
                connection, _rebuild_deck_id(connection)
            )
            _rename_deck(connection, deck_id, deck_name)
            note_type_id = _rebuild_note_type_id(connection)
            _empty_collection(connection)
            next_due = 1
            row_count = max(len(rows), 1)
            for index, row in enumerate(rows):
                progress(90 + 8 * index // row_count, "Собираю APKG")
                site_id, direction = _site_direction(row["Tags"])
                note_id = _stable_note_id(deck_id, site_id, direction)
                state = schedules.get((site_id, direction))
                if state is not None:
                    kind, queue, due, ivl, factor, reps, lapses, left, odue, odid = (
                        state[field] for field in _SCHEDULE_FIELDS
                    )
                else:
                    due = next_due + index
                    kind, queue, ivl, factor, reps, lapses, left, odue, odid = (
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                    )
                front = row["Front"]
                back = row["Back"]
                fields = front + "\x1f" + back
                tags = " rebuild " + " ".join(row["Tags"].split()) + " "
                connection.execute(
                    "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        note_id,
                        _rebuild_guid(front, deck_name),
                        note_type_id,
                        now_seconds,
                        -1,
                        tags,
                        fields,
                        plain_text(front),
                        checksum(front),
                        0,
                        "",
                    ),
                )
                connection.execute(
                    "INSERT INTO cards VALUES (?, ?, ?, 0, ?, -1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '{}')",
                    (
                        note_id,
                        note_id,
                        deck_id,
                        now_seconds,
                        kind,
                        queue,
                        due,
                        ivl,
                        factor,
                        reps,
                        lapses,
                        left,
                        odue,
                        odid,
                    ),
                )
            try:
                connection.execute(
                    "UPDATE config SET val = ?, usn = -1, mtime_secs = ? WHERE key = 'nextPos'",
                    (str(next_due + len(rows)), now_seconds),
                )
            except sqlite3.OperationalError:
                pass
            connection.execute("UPDATE col SET mod = ?", (now_seconds * 1000,))
            connection.commit()
        finally:
            connection.close()

        collection_name = "collection.anki21b" if is_compressed else "collection.anki2"
        if is_compressed:
            with collection_path.open("rb") as database_stream, (
                temporary / collection_name
            ).open("wb") as collection_stream:
                zstandard.ZstdCompressor(level=10).copy_stream(
                    database_stream, collection_stream
                )
            collection_path.unlink()
        media = temporary / "media"
        media.write_text("{}")
        members = [
            path
            for path in sorted(temporary.iterdir())
            if path.name == collection_name or path.name == "media"
        ]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for member in members:
                info = zipfile.ZipInfo(member.name)
                archive.writestr(info, member.read_bytes())
    progress(100, "Готово")
    return stream.getvalue()


# Public imports retained for existing CLI and library callers.
from articles_to_anki.rebuilding.collection import (
    _avoid_default_deck_id as _avoid_default_deck_id,
)
from articles_to_anki.rebuilding.collection import (
    _connect_collection as _connect_collection,
)
from articles_to_anki.rebuilding.collection import (
    _empty_collection as _empty_collection,
)
from articles_to_anki.rebuilding.collection import (
    _fallback_collection as _fallback_collection,
)
from articles_to_anki.rebuilding.collection import (
    _open_old_collection as _open_old_collection,
)
from articles_to_anki.rebuilding.collection import (
    _rebuild_note_type_id as _rebuild_note_type_id,
)
from articles_to_anki.rebuilding.collection import _rename_deck as _rename_deck
from articles_to_anki.rebuilding.constants import _COLLECTION_NAMES as _COLLECTION_NAMES
from articles_to_anki.rebuilding.constants import _DECK_REPLAY_END as _DECK_REPLAY_END
from articles_to_anki.rebuilding.constants import _SCHEDULE_FIELDS as _SCHEDULE_FIELDS
from articles_to_anki.rebuilding.constants import _TEMPLATE_JSON as _TEMPLATE_JSON
from articles_to_anki.rebuilding.constants import _ZSTD_MAGIC as _ZSTD_MAGIC
from articles_to_anki.rebuilding.constants import LOGGER as LOGGER
from articles_to_anki.rebuilding.constants import REBUILD_DECK_NAME as REBUILD_DECK_NAME
from articles_to_anki.rebuilding.constants import REBUILD_REVISION as REBUILD_REVISION
from articles_to_anki.rebuilding.constants import ProgressReporter as ProgressReporter
from articles_to_anki.rebuilding.contexts import _card_view as _card_view
from articles_to_anki.rebuilding.contexts import _docs_and_indexes as _docs_and_indexes
from articles_to_anki.rebuilding.contexts import (
    _mined_contexts_for_cluster as _mined_contexts_for_cluster,
)
from articles_to_anki.rebuilding.contexts import (
    _old_mined_contexts as _old_mined_contexts,
)
from articles_to_anki.rebuilding.identity import _cluster_id as _cluster_id
from articles_to_anki.rebuilding.identity import _deck_name as _deck_name
from articles_to_anki.rebuilding.identity import _rebuild_deck_id as _rebuild_deck_id
from articles_to_anki.rebuilding.identity import _rebuild_guid as _rebuild_guid
from articles_to_anki.rebuilding.identity import _stable_note_id as _stable_note_id
from articles_to_anki.rebuilding.schedules import _deck_names as _deck_names
from articles_to_anki.rebuilding.schedules import (
    _extract_schedules as _extract_schedules,
)
from articles_to_anki.rebuilding.schedules import (
    _resolve_source_decks as _resolve_source_decks,
)
from articles_to_anki.rebuilding.schedules import _site_direction as _site_direction
from articles_to_anki.rebuilding.seeds import (
    _cluster_candidate_models as _cluster_candidate_models,
)
from articles_to_anki.rebuilding.seeds import _cluster_candidates as _cluster_candidates
from articles_to_anki.rebuilding.seeds import _collect_seeds as _collect_seeds
from articles_to_anki.rebuilding.seeds import _highlight_entries as _highlight_entries
from articles_to_anki.rebuilding.seeds import (
    seed_analysis_from_context as seed_analysis_from_context,
)
from articles_to_anki.rebuilding.semantic import (
    rebuild_semantic_deck as rebuild_semantic_deck,
)
from articles_to_anki.rebuilding.types import HighlightEntry as HighlightEntry
from articles_to_anki.rebuilding.types import RebuildCluster as RebuildCluster
