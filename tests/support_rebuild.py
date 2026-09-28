from __future__ import annotations

import io
import json
import re
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path

from support_webapp import csrf

from articles_to_anki.security import encrypt_value, load_credential_keys

_REBUILD_DECK_NAME_RE = re.compile(
    r"^Anki Papers \(пересборка\) \d{4}-\d{2}-\d{2} \d{2}:\d{2}$"
)


_KEY = bytes(range(32))


def _full_legacy_collection_bytes(tmp_path: Path, site_id: str) -> bytes:
    database = sqlite3.connect(tmp_path / "full-old.sqlite")
    database.executescript(
        """
        CREATE TABLE col (
            id INTEGER PRIMARY KEY, crt INTEGER NOT NULL, mod INTEGER NOT NULL,
            scm INTEGER NOT NULL, ver INTEGER NOT NULL, dty INTEGER NOT NULL,
            usn INTEGER NOT NULL, ls INTEGER NOT NULL, conf TEXT NOT NULL,
            models TEXT NOT NULL, decks TEXT NOT NULL, dconf TEXT NOT NULL,
            tags TEXT NOT NULL
        );
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY, guid TEXT NOT NULL, mid INTEGER NOT NULL,
            mod INTEGER NOT NULL, usn INTEGER NOT NULL, tags TEXT NOT NULL,
            flds TEXT NOT NULL, sfld TEXT NOT NULL, csum INTEGER NOT NULL,
            flags INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY, nid INTEGER NOT NULL, did INTEGER NOT NULL,
            ord INTEGER NOT NULL, mod INTEGER NOT NULL, usn INTEGER NOT NULL,
            type INTEGER NOT NULL, queue INTEGER NOT NULL, due INTEGER NOT NULL,
            ivl INTEGER NOT NULL, factor INTEGER NOT NULL, reps INTEGER NOT NULL,
            lapses INTEGER NOT NULL, left INTEGER NOT NULL, odue INTEGER NOT NULL,
            odid INTEGER NOT NULL, flags INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE revlog (
            id INTEGER PRIMARY KEY, cid INTEGER NOT NULL, usn INTEGER NOT NULL,
            ease INTEGER NOT NULL, ivl INTEGER NOT NULL, lastIvl INTEGER NOT NULL,
            factor INTEGER NOT NULL, time INTEGER NOT NULL, type INTEGER NOT NULL
        );
        CREATE TABLE graves (
            id INTEGER PRIMARY KEY, oid INTEGER NOT NULL, type INTEGER NOT NULL,
            usn INTEGER NOT NULL
        );
        """
    )
    template = json.loads(
        (Path(__file__).parent.parent / "src/articles_to_anki/rebuild_template.json").read_text(
            encoding="utf-8"
        )
    )
    basic = next(mid for mid, model in template["models"].items() if len(model["flds"]) == 2)
    mod = int(time.time())
    database.execute(
        "INSERT INTO col VALUES (1, ?, ?, ?, ?, 0, -1, 0, ?, ?, ?, ?, ?)",
        (
            template["crt"],
            mod,
            mod * 1000,
            template["ver"],
            json.dumps(template["conf"]),
            json.dumps(template["models"]),
            json.dumps(template["decks"]),
            json.dumps(template["dconf"]),
            json.dumps(template["tags"]),
        ),
    )
    for index, (tags, kind, queue, due, ivl, factor, reps, lapses) in enumerate(
        [
            (f" anki_papers::{site_id} semantic::v1 card::meaning ", 2, 2, 3333, 25, 2500, 6, 1),
            (f" anki_papers::{site_id} semantic::v1 card::recall ", 3, 1, 4444, 7, 2200, 3, 0),
        ],
        start=1,
    ):
        database.execute(
            "INSERT INTO notes VALUES (?, 'g', ?, 1, 0, ?, 'front\\x1fback', 'front', 1, 0, '')",
            (index, basic, tags),
        )
        database.execute(
            "INSERT INTO cards VALUES (?, ?, 1, 0, 1, 0, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, '{}')",
            (index, index, kind, queue, due, ivl, factor, reps, lapses),
        )
    database.commit()
    database.close()
    content = (tmp_path / "full-old.sqlite").read_bytes()
    (tmp_path / "full-old.sqlite").unlink()
    return content


def _mirror_collection_bytes(tmp_path: Path, site_id: str) -> bytes:
    """Full modern collection with three decks and conflicting schedules.

    Deck "Default" carries the site's tags with wrong scheduling, deck
    "Papers" carries the correct meaning state and its subdeck "Papers::Advanced"
    the correct recall state. Choosing "Papers" must scope the transfer to the
    deck subtree and ignore "Default".
    """
    database = sqlite3.connect(tmp_path / "mirror-old.sqlite")
    database.executescript(
        """
        CREATE TABLE col (
            id INTEGER PRIMARY KEY, crt INTEGER NOT NULL, mod INTEGER NOT NULL,
            scm INTEGER NOT NULL, ver INTEGER NOT NULL, dty INTEGER NOT NULL,
            usn INTEGER NOT NULL, ls INTEGER NOT NULL, conf TEXT NOT NULL,
            models TEXT NOT NULL, decks TEXT NOT NULL, dconf TEXT NOT NULL,
            tags TEXT NOT NULL
        );
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY, guid TEXT NOT NULL, mid INTEGER NOT NULL,
            mod INTEGER NOT NULL, usn INTEGER NOT NULL, tags TEXT NOT NULL,
            flds TEXT NOT NULL, sfld TEXT NOT NULL, csum INTEGER NOT NULL,
            flags INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY, nid INTEGER NOT NULL, did INTEGER NOT NULL,
            ord INTEGER NOT NULL, mod INTEGER NOT NULL, usn INTEGER NOT NULL,
            type INTEGER NOT NULL, queue INTEGER NOT NULL, due INTEGER NOT NULL,
            ivl INTEGER NOT NULL, factor INTEGER NOT NULL, reps INTEGER NOT NULL,
            lapses INTEGER NOT NULL, left INTEGER NOT NULL, odue INTEGER NOT NULL,
            odid INTEGER NOT NULL, flags INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE revlog (
            id INTEGER PRIMARY KEY, cid INTEGER NOT NULL, usn INTEGER NOT NULL,
            ease INTEGER NOT NULL, ivl INTEGER NOT NULL, lastIvl INTEGER NOT NULL,
            factor INTEGER NOT NULL, time INTEGER NOT NULL, type INTEGER NOT NULL
        );
        CREATE TABLE graves (
            id INTEGER PRIMARY KEY, oid INTEGER NOT NULL, type INTEGER NOT NULL,
            usn INTEGER NOT NULL
        );
        """
    )
    template = json.loads(
        (Path(__file__).parent.parent / "src/articles_to_anki/rebuild_template.json").read_text(
            encoding="utf-8"
        )
    )
    basic = next(mid for mid, model in template["models"].items() if len(model["flds"]) == 2)
    base_deck = template["decks"]["1"]
    decks = {
        "1": {**base_deck, "name": "Default"},
        "2": {**base_deck, "name": "Papers"},
        "3": {**base_deck, "name": "Papers::Advanced"},
    }
    mod = int(time.time())
    database.execute(
        "INSERT INTO col VALUES (1, ?, ?, ?, ?, 0, -1, 0, ?, ?, ?, ?, ?)",
        (
            template["crt"],
            mod,
            mod * 1000,
            template["ver"],
            json.dumps(template["conf"]),
            json.dumps(template["models"]),
            json.dumps(decks),
            json.dumps(template["dconf"]),
            json.dumps(template["tags"]),
        ),
    )
    for index, (tags, kind, queue, due, ivl, factor, reps, lapses, deck) in enumerate(
        [
            (f" anki_papers::{site_id} semantic::v1 card::meaning ", 2, 2, 1111, 90, 2500, 20, 5, 1),
            (f" anki_papers::{site_id} semantic::v1 card::recall ", 3, 1, 2222, 3, 2200, 1, 0, 1),
            (f" anki_papers::{site_id} semantic::v1 card::meaning ", 2, 2, 3333, 25, 2500, 6, 1, 2),
            (f" anki_papers::{site_id} semantic::v1 card::recall ", 3, 1, 4444, 7, 2200, 3, 0, 3),
        ],
        start=1,
    ):
        database.execute(
            "INSERT INTO notes VALUES (?, 'g', ?, 1, 0, ?, 'front\\x1fback', 'front', 1, 0, '')",
            (index, basic, tags),
        )
        database.execute(
            "INSERT INTO cards VALUES (?, ?, ?, 0, 1, 0, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, '{}')",
            (index, index, deck, kind, queue, due, ivl, factor, reps, lapses),
        )
    database.commit()
    database.close()
    content = (tmp_path / "mirror-old.sqlite").read_bytes()
    (tmp_path / "mirror-old.sqlite").unlink()
    return content


def _install_ankiweb_mirror(tmp_path: Path, site_id: str) -> None:
    """Persist an encrypted mirror and its deck list for the test user (id 1)."""
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        user_id = database.execute("SELECT id FROM users").fetchone()[0]
        keys = load_credential_keys()
        encrypted = encrypt_value(
            _mirror_collection_bytes(tmp_path, site_id),
            user_id=user_id,
            field="collection_mirror",
            keys=keys,
        )
        mirror_path = tmp_path / "mirror.enc"
        mirror_path.write_bytes(encrypted.ciphertext)
        decks = json.dumps(
            [
                {"id": 1, "name": "Default"},
                {"id": 2, "name": "Papers"},
                {"id": 3, "name": "Papers::Advanced"},
            ],
            ensure_ascii=False,
        )
        database.execute(
            """INSERT INTO anki_accounts
               (user_id, available_decks_json, mirror_path, mirror_nonce,
                mirror_key_version, state, selected_deck_id, selected_deck_name,
                updated_at)
               VALUES (?, ?, ?, ?, ?, 'connected', 2, 'Papers', '2026-01-01')""",
            (
                user_id,
                decks,
                str(mirror_path),
                encrypted.nonce,
                encrypted.key_version,
            ),
        )
        database.commit()


def _add_highlight(client, document_id: str, target: str = "robust", sentence: str = "This is a robust result.") -> None:
    reader = client.get(f"/article/{document_id}")
    response = client.post(
        f"/api/article/{document_id}/highlights",
        json={
            "id": str(uuid.uuid4()),
            "target": target,
            "sentence": sentence,
            "page": 1,
            "rects": [{"x1": 80, "y1": 190, "x2": 120, "y2": 205}],
        },
        headers={"X-CSRF-Token": csrf(reader)},
    )
    assert response.status_code == 200


def _site_card_id(tmp_path: Path) -> str:
    with sqlite3.connect(tmp_path / "app.sqlite3") as database:
        return str(
            database.execute(
                "SELECT id FROM cards WHERE semantic_version = 1 ORDER BY created_at"
            ).fetchone()[0]
        )


def _read_rebuilt(tmp_path: Path, response, collection_name: str = "collection.anki2") -> sqlite3.Connection:
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    assert "media" in archive.namelist()
    path = tmp_path / "rebuilt.sqlite"
    path.write_bytes(archive.read(collection_name))
    return sqlite3.connect(path)


def _start_rebuild(client, tmp_path: Path, **extra) -> None:
    response = client.post(
        "/export/rebuild",
        data={"csrf_token": csrf(client.get("/settings")), **extra},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/settings#rebuild"
