from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from anki.collection import Collection
from anki.decks import DeckId
from anki_papers_sync_worker.official import OfficialAnkiAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_ANKI_SYNC_INTEGRATION") != "1",
    reason="set RUN_ANKI_SYNC_INTEGRATION=1",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int) -> None:
    for _ in range(50):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("sync server did not start")


def add_basic_note(collection: Collection, front: str, back: str):
    note = collection.new_note(collection.models.by_name("Basic"))
    note["Front"] = front
    note["Back"] = back
    collection.add_note(note, DeckId(1))
    return note


def full_upload_fixture(collection: Collection, auth) -> None:
    output = collection.sync_collection(auth, sync_media=False)
    assert output.required == output.FULL_UPLOAD
    collection.close_for_full_sync()
    collection.full_upload_or_download(auth=auth, server_usn=None, upload=True)
    collection.reopen(after_full_sync=True)


def test_official_disposable_sync_server_preserves_personal_cards(tmp_path: Path) -> None:
    port = free_port()
    endpoint = f"http://127.0.0.1:{port}/"
    environment = {
        **os.environ,
        "SYNC_USER1": "worker-user:worker-password",
        "SYNC_BASE": str(tmp_path / "server"),
        "SYNC_HOST": "127.0.0.1",
        "SYNC_PORT": str(port),
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "anki.syncserver"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_port(port)
        desktop_path = tmp_path / "desktop.anki2"
        desktop = Collection(str(desktop_path))
        personal = add_basic_note(desktop, "personal front", "personal back")
        personal_card = personal.cards()[0]
        personal_state = (personal.id, personal_card.id, personal_card.due, personal_card.ivl)
        auth = desktop.sync_login("worker-user", "worker-password", endpoint=endpoint)
        full_upload_fixture(desktop, auth)
        desktop.close()

        cards = [
            {
                "id": "context-1",
                "target": "robust",
                "sentence": "A robust result.",
                "replacement": "надёжный",
                "translations": ["надёжный", "стойкий"],
                "alternatives": ["strong", "durable"],
                "document_name": "paper.pdf",
                "page": 1,
            },
            {
                "id": "context-2",
                "target": "robust",
                "sentence": "A robust implementation.",
                "replacement": "надёжная",
                "translations": ["надёжная", "стойкая"],
                "alternatives": ["strong", "durable"],
                "document_name": "paper.pdf",
                "page": 2,
            },
        ]
        adapter = OfficialAnkiAdapter(endpoint)
        mirror = tmp_path / "mirror.anki2"
        connected = adapter.connect(
            mirror, "worker-user", "worker-password", cards, []
        )
        assert connected.existing == 0
        assert connected.missing == 4
        first = adapter.sync(mirror, connected.hkey, 1, cards, connected.links)
        assert first.added == 4

        desktop = Collection(str(desktop_path))
        output = desktop.sync_collection(auth, sync_media=False)
        assert output.required == output.NO_CHANGES
        managed_id = desktop.find_notes(
            'tag:"anki_papers::context-1" tag:"direction::meaning"'
        )[0]
        edited_managed = desktop.get_note(managed_id)
        edited_managed["Front"] = "user edited managed note"
        desktop.update_note(edited_managed)
        add_basic_note(desktop, "created on another client", "must download first")
        output = desktop.sync_collection(auth, sync_media=False)
        assert output.required == output.NO_CHANGES
        desktop.close()

        second = adapter.sync(mirror, connected.hkey, 1, cards, first.links)
        assert second.added == 0
        mirrored = Collection(str(mirror))
        downloaded = mirrored.find_notes('"created on another client"')
        assert len(downloaded) == 1
        assert mirrored.get_note(downloaded[0])["Front"] == "created on another client"
        mirrored.close()

        desktop = Collection(str(desktop_path))
        output = desktop.sync_collection(auth, sync_media=False)
        assert output.required == output.NO_CHANGES
        personal_after = desktop.get_note(personal_state[0])
        card_after = desktop.get_card(personal_state[1])
        assert personal_after["Front"] == "personal front"
        assert personal_after["Back"] == "personal back"
        assert (card_after.due, card_after.ivl) == personal_state[2:]
        assert len(desktop.find_notes('tag:"anki_papers::*"')) == 4
        assert len(desktop.find_notes('tag:"anki_papers::context-1"')) == 2
        assert len(desktop.find_notes('tag:"anki_papers::context-2"')) == 2
        assert desktop.get_note(managed_id)["Front"] == "user edited managed note"
        desktop.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
