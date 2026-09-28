from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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



from .errors import AuthenticationError, PermanentSyncError, RetryableSyncError
from .notes import NoteReconciler


class OfficialAnkiAdapter(NoteReconciler):
    """Sync transport. Full upload has no call path."""

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

    def import_rebuild(
        self,
        collection_path: Path,
        apkg_path: Path,
        hkey: str,
    ) -> AdapterResult:
        """Import a freshly rebuilt deck package and push it to AnkiWeb.

        The mirror collection is brought up to date, the APKG is imported (its
        GUIDs and schedules deduplicate on re-import), and the result is
        uploaded back. Rebuilt notes are tagged `rebuild`, so the regular
        reconcile never touches them again.
        """
        collection = None
        try:
            from anki.collection import Collection
            from anki.import_export_pb2 import (
                ImportAnkiPackageOptions,
                ImportAnkiPackageRequest,
            )
            from anki.sync import SyncAuth

            collection = Collection(str(collection_path))
            auth = SyncAuth(hkey=hkey, endpoint=self.endpoint)
            self._normal_or_download(collection, auth)
            for _ in range(3):
                collection.import_anki_package(
                    ImportAnkiPackageRequest(
                        package_path=str(apkg_path),
                        options=ImportAnkiPackageOptions(
                            merge_notetypes=True,
                            with_scheduling=True,
                            with_deck_configs=False,
                        ),
                    )
                )
                downloaded = self._normal_or_download(collection, auth)
                if not downloaded:
                    break
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
            return AdapterResult(hkey=hkey, decks=decks, links=[])
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

from .rendering import _semantic_sides as _semantic_sides
