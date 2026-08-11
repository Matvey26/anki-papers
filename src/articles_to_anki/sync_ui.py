from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")

JOB_REASONS = {
    "connect": "Подключение AnkiWeb",
    "initial_sync": "Первая синхронизация",
    "manual": "Ручная синхронизация",
    "card_saved": "Новые карточки",
}

JOB_STATES = {
    "queued": "Запланировано",
    "running": "Выполняется",
    "succeeded": "Готово",
    "failed": "Ошибка",
    "cancelled": "Отменено",
}


def _value(row: Any | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, TypeError, IndexError):
        return default
    return default if value is None else value


def format_sync_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return "—"
    return parsed.astimezone(MOSCOW).strftime("%d.%m.%Y, %H:%M МСК")


def sync_job_reason(value: str) -> str:
    return JOB_REASONS.get(value, "Фоновая синхронизация")


def sync_job_state(value: str) -> str:
    return JOB_STATES.get(value, "Неизвестное состояние")


def sync_error_text(code: str | None) -> str | None:
    if not code:
        return None
    if code == "auth" or code.startswith("auth:"):
        return "AnkiWeb отклонил вход. Переподключите аккаунт."
    if code.startswith("configuration:remote_collection_empty"):
        return "Коллекция AnkiWeb пуста. Сначала загрузите её из Anki Desktop."
    if code.startswith("configuration:deck_not_selected"):
        return "Целевая колода не выбрана. Выберите её выше."
    if code.startswith("configuration:managed_notetype_invalid"):
        return "Тип карточек «Anki Papers» изменён вручную и несовместим."
    if code == "configuration" or code.startswith("configuration:"):
        return "Коллекция требует ручной проверки. Повторите синхронизацию после проверки Anki."
    if code.startswith("temporary:") or code == "temporary":
        return "AnkiWeb или сеть временно недоступны. Повтор выполняется автоматически."
    if code.startswith("internal:") or code == "internal":
        return "Внутренняя ошибка сервиса. Запустите повтор; если ошибка вернётся — нужен разбор логов."
    if code == "coalesced":
        return "Задание объединено с более новой синхронизацией."
    return "Синхронизация завершилась с ошибкой."


def build_sync_status(
    account: Any | None,
    active_job: Any | None,
    latest_job: Any | None,
    pending_words: int,
) -> dict[str, Any]:
    pending_words = int(pending_words)
    status: dict[str, Any] = {
        "tone": "neutral",
        "title": "AnkiWeb не подключён",
        "message": "Подключите аккаунт, чтобы карточки из PDF автоматически появлялись в Anki.",
        "stage": "Не настроено",
        "progress": 0,
        "active": False,
        "pending_words": pending_words,
        "pending_notes": pending_words * 2,
    }
    if account is None:
        return status

    account_state = str(_value(account, "state", "error"))
    selected_deck = _value(account, "selected_deck_name")
    last_success_at = _value(account, "last_success_at")
    last_added_count = int(_value(account, "last_added_count", 0))
    status.update(
        selected_deck=selected_deck,
        last_success_at=last_success_at,
        last_success_label=format_sync_time(last_success_at),
        last_added_count=last_added_count,
    )

    if active_job is not None:
        job_state = str(_value(active_job, "state", "queued"))
        reason = str(_value(active_job, "reason", "manual"))
        if job_state == "queued":
            status.update(
                tone="progress",
                title="Синхронизация запланирована",
                message="Задание в очереди. Страница обновится автоматически после запуска.",
                stage="В очереди",
                progress=20 if reason == "connect" else 70,
                active=True,
            )
        else:
            connecting = reason == "connect" or account_state == "connecting"
            status.update(
                tone="progress",
                title="Подключаем AnkiWeb" if connecting else "Синхронизация идёт",
                message=(
                    "Проверяем вход и скачиваем коллекцию AnkiWeb."
                    if connecting
                    else "Сверяем сохранённые слова и обновляем целевую колоду."
                ),
                stage="Загрузка коллекции" if connecting else "Сверка карточек",
                progress=40 if connecting else 82,
                active=True,
            )
        return status

    if account_state == "awaiting_deck":
        status.update(
            tone="action",
            title="Выберите целевую колоду",
            message="Коллекция AnkiWeb загружена. Остался один шаг перед первой синхронизацией.",
            stage="Коллекция загружена",
            progress=60,
        )
    elif account_state == "syncing":
        status.update(
            tone="progress",
            title="Синхронизация идёт",
            message="Сверяем сохранённые слова и обновляем целевую колоду.",
            stage="Сверка карточек",
            progress=82,
            active=True,
        )
    elif account_state == "needs_reconnect":
        status.update(
            tone="error",
            title="Нужно переподключить AnkiWeb",
            message="AnkiWeb дважды отклонил авторизацию. Подключите аккаунт заново.",
            stage="Остановлено: вход",
            progress=15,
        )
    elif account_state == "error":
        error_code = _value(latest_job, "error_code")
        status.update(
            tone="error",
            title="Синхронизация остановлена",
            message=_value(account, "last_error", "Синхронизация завершилась с ошибкой."),
            error_detail=sync_error_text(error_code),
            stage="Требуется действие",
            progress=75,
        )
    elif account_state == "connected":
        status.update(
            tone="success",
            title="AnkiWeb синхронизирован",
            message=(
                f"Ожидают отправки: {pending_words} сохранённых слов, {pending_words * 2} карточек."
                if pending_words
                else "Все сохранённые слова отправлены в AnkiWeb."
            ),
            stage="Готово",
            progress=100,
        )
    else:
        status.update(
            tone="progress",
            title="Подключаем AnkiWeb",
            message="Проверяем вход и скачиваем коллекцию AnkiWeb.",
            stage="Загрузка коллекции",
            progress=40,
            active=True,
        )
    return status
