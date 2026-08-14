from __future__ import annotations

import hashlib
import html
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AuthenticationError(RuntimeError):
    pass


class RetryableSyncError(RuntimeError):
    pass


class PermanentSyncError(RuntimeError):
    pass


MANAGED_NOTETYPE_NAME = "Anki Papers"
SEMANTIC_NOTETYPE_NAME = "Anki Papers Semantic"


@dataclass(frozen=True)
class AdapterResult:
    hkey: str
    decks: list[dict[str, Any]]
    links: list[dict[str, Any]]
    existing: int = 0
    missing: int = 0
    added: int = 0


class OfficialAnkiAdapter:
    """Only module importing AGPL Anki code. Full upload has no call path."""

    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint or os.environ.get("ANKI_SYNC_ENDPOINT") or None

    def connect(
        self,
        collection_path: Path,
        username: str,
        password: str,
        cards: list[dict[str, Any]],
        known_links: list[dict[str, Any]] | None = None,
    ) -> AdapterResult:
        collection = None
        try:
            from anki.collection import Collection

            collection = Collection(str(collection_path))
            auth = collection.sync_login(username, password, endpoint=self.endpoint)
            output = collection.sync_collection(auth, sync_media=False)
            self._apply_new_endpoint(auth, output)
            if output.required == output.FULL_UPLOAD:
                collection.close()
                raise PermanentSyncError("remote_collection_empty")
            if output.required != output.NO_CHANGES:
                self._full_download(collection, auth)
            links, existing, missing = self._reconcile(
                collection, cards, add=False, deck_id=None, known_links=known_links
            )
            decks = [
                {"id": int(deck.id), "name": str(deck.name)}
                for deck in collection.decks.all_names_and_ids(include_filtered=False)
            ]
            collection.close()
            return AdapterResult(
                hkey=auth.hkey,
                decks=decks,
                links=links,
                existing=existing,
                missing=missing,
            )
        except (AuthenticationError, PermanentSyncError):
            raise
        except Exception as exc:  # noqa: BLE001 - classify backend errors without logging secrets
            self._raise_classified(exc)
        finally:
            self._safe_close(collection)

    def login(self, collection_path: Path, username: str, password: str) -> str:
        collection = None
        try:
            from anki.collection import Collection

            collection = Collection(str(collection_path))
            auth = collection.sync_login(username, password, endpoint=self.endpoint)
            collection.close()
            return str(auth.hkey)
        except Exception as exc:  # noqa: BLE001 - classify backend errors without logging secrets
            self._raise_classified(exc)
        finally:
            self._safe_close(collection)

    def sync(
        self,
        collection_path: Path,
        hkey: str,
        deck_id: int,
        cards: list[dict[str, Any]],
        known_links: list[dict[str, Any]] | None = None,
    ) -> AdapterResult:
        collection = None
        try:
            from anki.collection import Collection
            from anki.sync import SyncAuth

            collection = Collection(str(collection_path))
            auth = SyncAuth(hkey=hkey, endpoint=self.endpoint)
            self._normal_or_download(collection, auth)
            links, _existing, _missing, added = self._reconcile_and_add(
                collection, cards, deck_id, known_links
            )
            for _ in range(3):
                downloaded = self._normal_or_download(collection, auth)
                if not downloaded:
                    break
                links, _existing, _missing, newly_added = self._reconcile_and_add(
                    collection, cards, deck_id, known_links
                )
                added = newly_added
            else:
                collection.close()
                raise RetryableSyncError("repeated_full_sync")
            status = collection.sync_status(auth)
            if status.required != status.NO_CHANGES:
                collection.close()
                raise RetryableSyncError("remote_changed_during_sync")
            decks = [
                {"id": int(deck.id), "name": str(deck.name)}
                for deck in collection.decks.all_names_and_ids(include_filtered=False)
            ]
            collection.close()
            return AdapterResult(hkey=hkey, decks=decks, links=links, added=added)
        except (AuthenticationError, PermanentSyncError, RetryableSyncError):
            raise
        except Exception as exc:  # noqa: BLE001 - classify backend errors without logging secrets
            self._raise_classified(exc)
        finally:
            self._safe_close(collection)

    def _normal_or_download(self, collection: Any, auth: Any) -> bool:
        output = collection.sync_collection(auth, sync_media=False)
        self._apply_new_endpoint(auth, output)
        if output.required == output.NO_CHANGES:
            return False
        self._full_download(collection, auth)
        return True

    @staticmethod
    def _apply_new_endpoint(auth: Any, output: Any) -> None:
        if endpoint := output.new_endpoint:
            auth.endpoint = endpoint

    @staticmethod
    def _full_download(collection: Any, auth: Any) -> None:
        collection.close_for_full_sync()
        collection.full_upload_or_download(auth=auth, server_usn=None, upload=False)
        collection.reopen(after_full_sync=True)

    def _reconcile_and_add(
        self,
        collection: Any,
        cards: list[dict[str, Any]],
        deck_id: int,
        known_links: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        links, existing, missing = self._reconcile(
            collection, cards, add=True, deck_id=deck_id, known_links=known_links
        )
        return links, existing, missing, missing

    def _reconcile(
        self,
        collection: Any,
        cards: list[dict[str, Any]],
        *,
        add: bool,
        deck_id: int | None,
        known_links: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        links: list[dict[str, Any]] = []
        used_note_ids: set[int] = set()
        linked_note_ids = {
            (link["site_card_id"], link["direction"]): int(link["note_id"])
            for link in (known_links or [])
        }
        existing = 0
        missing = 0
        for card in cards:
            for direction in ("meaning", "recall"):
                note = None
                linked_id = linked_note_ids.get((card["id"], direction))
                if linked_id is not None and linked_id not in used_note_ids:
                    from anki.errors import NotFoundError

                    try:
                        note = collection.get_note(linked_id)
                    except NotFoundError:
                        note = None
                if note is None:
                    note = self._find_note(collection, card, direction, used_note_ids)
                if note is None:
                    missing += 1
                    if add:
                        if deck_id is None:
                            raise PermanentSyncError("deck_not_selected")
                        note = self._add_note(collection, card, direction, deck_id)
                else:
                    existing += 1
                if note is not None:
                    if add:
                        if card.get("semantic"):
                            self._update_semantic_note(collection, note, card, direction)
                        self._ensure_sync_tags(collection, note, card["id"], direction)
                    used_note_ids.add(int(note.id))
                    links.append(
                        {
                            "site_card_id": card["id"],
                            "direction": direction,
                            "note_id": int(note.id),
                            "note_guid": str(note.guid),
                        }
                    )
        return links, existing, missing

    def _find_note(
        self,
        collection: Any,
        card: dict[str, Any],
        direction: str,
        used_note_ids: set[int],
    ) -> Any | None:
        stable_tag = f"anki_papers::{card['id']}"
        searches = [
            f'tag:"{stable_tag}" tag:"direction::{direction}"',
            f'tag:"{stable_tag}" tag:"card::{direction}"',
        ]
        for query in searches:
            ids = collection.find_notes(query)
            for note_id in ids:
                if int(note_id) not in used_note_ids:
                    return collection.get_note(note_id)
        legacy_ids = collection.find_notes(
            f'tag:"card::{direction}" OR tag:"direction::{direction}"'
        )
        for note_id in legacy_ids:
            if int(note_id) in used_note_ids:
                continue
            note = collection.get_note(note_id)
            if self._legacy_matches(note, card, direction):
                return note
        return None

    @staticmethod
    def _ensure_sync_tags(collection: Any, note: Any, card_id: str, direction: str) -> None:
        """Make a reconciled legacy note identifiable without changing its content or schedule."""
        required = (f"anki_papers::{card_id}", f"direction::{direction}")
        current = list(note.tags)
        if all(tag in current for tag in required):
            return
        note.tags = [*current, *(tag for tag in required if tag not in current)]
        collection.update_note(note)

    @staticmethod
    def _legacy_matches(note: Any, card: dict[str, Any], direction: str) -> bool:
        fields = list(note.fields)
        if len(fields) < 2:
            return False
        front = _plain(fields[0])
        back = _plain(fields[1])
        target = _normalized(card["target"])
        sentence = _normalized(card["sentence"])
        if direction == "meaning":
            return front == sentence and target in front
        expected = _normalized(
            re.sub(
                re.escape(card["target"]),
                card["replacement"],
                card["sentence"],
                count=1,
                flags=re.IGNORECASE,
            )
        )
        return back == target and front.startswith(expected)

    @staticmethod
    def _add_note(collection: Any, card: dict[str, Any], direction: str, deck_id: int) -> Any:
        from anki.decks import DeckId
        from anki.utils import base91

        notetype = (
            OfficialAnkiAdapter._semantic_notetype(collection)
            if card.get("semantic")
            else OfficialAnkiAdapter._managed_notetype(collection)
        )
        note = collection.new_note(notetype)
        note.guid = base91(
            int.from_bytes(
                hashlib.sha256(f"{card['id']}:{direction}".encode()).digest()[:8],
                "big",
            )
        )
        front, back = _card_sides(card, direction)
        note.fields[0] = front
        note.fields[1] = back
        note.tags = [f"anki_papers::{card['id']}", f"direction::{direction}"]
        collection.add_note(note, DeckId(deck_id))
        return note

    @staticmethod
    def _update_semantic_note(collection: Any, note: Any, card: dict[str, Any], direction: str) -> None:
        front, back = _card_sides(card, direction)
        if list(note.fields[:2]) == [front, back]:
            return
        note.fields[0] = front
        note.fields[1] = back
        collection.update_note(note)

    @staticmethod
    def _managed_notetype(collection: Any) -> Any:
        notetype = collection.models.by_name(MANAGED_NOTETYPE_NAME)
        if notetype is not None:
            field_names = [field["name"] for field in notetype["flds"]]
            if field_names[:2] != ["Front", "Back"] or not notetype["tmpls"]:
                raise PermanentSyncError("managed_notetype_invalid")
            return notetype

        notetype = collection.models.new(MANAGED_NOTETYPE_NAME)
        collection.models.add_field(notetype, collection.models.new_field("Front"))
        collection.models.add_field(notetype, collection.models.new_field("Back"))
        template = collection.models.new_template("Card 1")
        template["qfmt"] = "{{Front}}"
        template["afmt"] = '{{FrontSide}}<hr id="answer">{{Back}}'
        collection.models.add_template(notetype, template)
        collection.models.add(notetype)
        return notetype

    @staticmethod
    def _semantic_notetype(collection: Any) -> Any:
        notetype = collection.models.by_name(SEMANTIC_NOTETYPE_NAME)
        if notetype is not None:
            names = [field["name"] for field in notetype["flds"]]
            if names[:2] != ["Front", "Back"] or not notetype["tmpls"]:
                raise PermanentSyncError("semantic_notetype_invalid")
            return notetype
        notetype = collection.models.new(SEMANTIC_NOTETYPE_NAME)
        collection.models.add_field(notetype, collection.models.new_field("Front"))
        collection.models.add_field(notetype, collection.models.new_field("Back"))
        template = collection.models.new_template("Card 1")
        template["qfmt"] = "{{Front}}"
        template["afmt"] = '{{FrontSide}}<hr id="answer">{{Back}}'
        collection.models.add_template(notetype, template)
        collection.models.add(notetype)
        return notetype

    @staticmethod
    def _raise_classified(exc: Exception) -> None:
        try:
            from anki.errors import SyncError, SyncErrorKind

            if isinstance(exc, SyncError) and exc.kind is SyncErrorKind.AUTH:
                raise AuthenticationError("ankiweb_auth") from None
        except ImportError:
            pass
        raise RetryableSyncError(type(exc).__name__) from None

    @staticmethod
    def _safe_close(collection: Any | None) -> None:
        if collection is None:
            return
        try:
            collection.close()
        except Exception:  # noqa: BLE001, S110 - close errors contain backend context
            pass


def _card_sides(card: dict[str, Any], direction: str) -> tuple[str, str]:
    if card.get("semantic"):
        return _semantic_sides(card, direction)
    translations = card["translations"]
    alternatives = card["alternatives"]
    if direction == "meaning":
        front = _replace_target(card["sentence"], card["target"], html.escape(card["target"]))
        back = "<br>".join(f"• {html.escape(value)}" for value in translations)
        return front, back
    replacement = f"<b>{html.escape(card['replacement'])}</b>"
    front = _replace_target(card["sentence"], card["target"], replacement, raw=True)
    if alternatives:
        front += "<br><small>Нельзя использовать: " + ", ".join(
            html.escape(value) for value in alternatives
        ) + "</small>"
    return front, f"<b>{html.escape(card['target'])}</b>"


def _semantic_sides(card: dict[str, Any], direction: str) -> tuple[str, str]:
    contexts = card.get("contexts") or []
    if not contexts:
        raise PermanentSyncError("semantic_card_without_contexts")
    values = []
    for context in contexts:
        target = str(context["target"])
        sentence = str(context["sentence"])
        values.append({
            "front": _replace_target(sentence, target, html.escape(target)),
            "recall": _replace_target(sentence, target, "<b>[...]</b>", raw=True),
            "translation": html.escape(str(context["replacement"])),
            "source": html.escape(str(context["source"])),
        })
    payload = html.escape(json.dumps(values, ensure_ascii=False), quote=True)
    side = "front" if direction == "meaning" else "recall"
    key = html.escape(str(card["id"]), quote=True)
    front = _semantic_front_html(payload, key, side, values[0])
    lemma = html.escape(str(card["lemma"]))
    if direction == "meaning":
        translations = "<br>".join(f"• {html.escape(str(item))}" for item in card["translations"])
        return front, f"<b>{lemma}</b><br>{translations}<br><small>{html.escape(str(card['sense_definition_en']))}</small>"
    return front, f"<b>{lemma}</b>"


def _semantic_front_html(
    payload: str,
    key: str,
    side: str,
    fallback: dict[str, str],
) -> str:
    fallback_html = (
        f'{fallback[side]}<br><small>{fallback["translation"]} · '
        f'{fallback["source"]}</small>'
    )
    return (
        f'<div class="anki-papers-semantic" data-key="{key}" data-side="{side}" '
        f'data-contexts="{payload}">{fallback_html}</div>'
        '<script>(function(){var root=document.currentScript.previousElementSibling,items=JSON.parse(root.dataset.contexts),'
        'key="anki-papers-context:"+root.dataset.key+":"+root.dataset.side,stored=null;'
        'try{stored=JSON.parse(sessionStorage.getItem(key)||"null")}catch(e){}var now=Date.now(),'
        'valid=stored&&stored.until>now&&Number.isInteger(stored.index)&&stored.index>=0&&'
        'stored.index<items.length,index=valid?stored.index:Math.floor(Math.random()*items.length);'
        'try{sessionStorage.setItem(key,JSON.stringify({index:index,until:now+120000}))}catch(e){}'
        'var item=items[index];root.innerHTML=item[root.dataset.side]+"<br><small>"+item.translation+" · "+item.source+"</small>";})();</script>'
    )


def _replace_target(sentence: str, target: str, replacement: str, raw: bool = False) -> str:
    match = re.search(re.escape(target), sentence, flags=re.IGNORECASE)
    if not match:
        return html.escape(sentence)
    selected = replacement if raw else f"<b>{replacement}</b>"
    return html.escape(sentence[: match.start()]) + selected + html.escape(sentence[match.end() :])


def _plain(value: str) -> str:
    return _normalized(html.unescape(re.sub(r"<[^>]+>", " ", value)))


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())
