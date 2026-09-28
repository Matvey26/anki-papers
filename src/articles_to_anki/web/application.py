"""web / application."""
from __future__ import annotations

import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from articles_to_anki.enrichment.targets import load_env_file
from articles_to_anki.quick_dictionary import StarDictDictionary
from articles_to_anki.security import load_credential_keys
from articles_to_anki.storage.database import close_database
from articles_to_anki.storage.schema import init_database


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    load_env_file(Path.cwd() / ".env")
    app = Flask("articles_to_anki.webapp")
    data_dir = Path(os.environ.get("ANKI_PAPERS_DATA_DIR", "data")).resolve()
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("APP_SECRET") or secrets.token_hex(32),
        DATA_DIR=data_dir,
        DATABASE=data_dir / "app.sqlite3",
        MAX_CONTENT_LENGTH=85 * 1024 * 1024,
        AUTO_PROCESS_UPLOADS=True,
        PROCESS_DOCUMENTS_INLINE=False,
        REBUILD_INLINE=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=True,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        ANKIWEB_ALLOWED_USERS=os.environ.get("ANKIWEB_ALLOWED_USERS", "risesduckness"),
        ANKI_CREDENTIAL_KEYS=load_credential_keys(),
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    init_database(app)
    app.extensions["highlight_executor"] = ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="pdf-highlights",
    )
    app.extensions["highlight_jobs"] = set()
    app.extensions["highlight_jobs_lock"] = threading.Lock()
    app.extensions["reader_highlight_jobs"] = set()
    app.extensions["quick_translation_cache"] = {}
    app.extensions["quick_translation_cache_lock"] = threading.Lock()
    app.extensions["quick_translation_dictionary"] = StarDictDictionary.load(
        Path(app.config["DATA_DIR"]) / "dictionaries" / "eng-rus"
    )
    app.extensions["deleted_document_paths"] = {}
    app.extensions["highlight_resume_done"] = False
    app.extensions["rebuild_executor"] = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="deck-rebuild",
    )
    app.extensions["rebuild_jobs"] = set()
    app.extensions["rebuild_jobs_lock"] = threading.Lock()

    from articles_to_anki.web.hooks import register as register_hooks
    register_hooks(app)

    from articles_to_anki.web.routes.health import register as register_health
    register_health(app)

    from articles_to_anki.web.routes.auth import register as register_auth
    register_auth(app)

    from articles_to_anki.web.routes.library import register as register_library
    register_library(app)

    from articles_to_anki.web.routes.settings import register as register_settings
    register_settings(app)

    from articles_to_anki.web.routes.documents import register as register_documents
    register_documents(app)

    from articles_to_anki.web.routes.highlights import register as register_highlights
    register_highlights(app)

    from articles_to_anki.web.routes.exports import register as register_exports
    register_exports(app)

    app.teardown_appcontext(close_database)
    return app
