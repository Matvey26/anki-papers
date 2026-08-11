from __future__ import annotations

from articles_to_anki.sync_ui import (
    build_sync_status,
    format_sync_time,
    sync_error_text,
    sync_job_reason,
    sync_job_state,
)


def test_sync_status_explains_card_and_word_counts() -> None:
    account = {
        "state": "connected",
        "selected_deck_name": "Papers",
        "last_success_at": "2026-08-11T09:35:27+00:00",
        "last_added_count": 8,
    }

    status = build_sync_status(account, None, None, pending_words=3)

    assert status["title"] == "AnkiWeb синхронизирован"
    assert status["pending_words"] == 3
    assert status["pending_notes"] == 6
    assert "3 сохранённых слов, 6 карточек" in status["message"]
    assert status["selected_deck"] == "Papers"


def test_active_and_failed_jobs_have_actionable_russian_labels() -> None:
    account = {"state": "error", "last_error": "Синхронизация остановлена."}
    queued = {"state": "queued", "reason": "card_saved"}
    active = build_sync_status(account, queued, queued, pending_words=2)
    assert active["title"] == "Синхронизация запланирована"
    assert active["active"] is True

    failed = {"state": "failed", "error_code": "temporary:SyncError"}
    stopped = build_sync_status(account, None, failed, pending_words=2)
    assert stopped["tone"] == "error"
    assert "временно недоступны" in stopped["error_detail"]
    assert sync_job_reason("initial_sync") == "Первая синхронизация"
    assert sync_job_state("failed") == "Ошибка"
    assert "переподключите" in sync_error_text("auth").lower()


def test_sync_time_is_shown_in_moscow_timezone() -> None:
    assert format_sync_time("2026-08-11T09:35:27+00:00") == "11.08.2026, 12:35 МСК"
