# Anki Papers Sync Worker

Отдельный AGPL-3.0-or-later process. Использует официальный headless
`anki==26.5`; web-приложение не импортирует этот package.

Worker последовательно берёт `sync_jobs` из общей SQLite, расшифровывает
per-user mirror во временный каталог `0700`, создаёт локальный backup, делает
normal sync, reconciliation и второй normal sync. Media sync выключен.
Оставшиеся после crash временные коллекции удаляются startup cleaner.

Full upload запрещён структурно: единственный вызов
`full_upload_or_download()` содержит `upload=False`.

Логи worker не содержат request bodies, AnkiWeb credentials, `hkey`, collection
contents или backend exception text. В SQLite сохраняются только стабильные
error codes и безопасные пользовательские сообщения.

```bash
python -m pip install -e .
ANKI_CREDENTIAL_KEY='1:BASE64_32_BYTE_KEY' \
  anki-papers-sync-worker --data-dir ../data
```

Upstream и лицензия: [NOTICE.md](NOTICE.md), [LICENSE](LICENSE).
