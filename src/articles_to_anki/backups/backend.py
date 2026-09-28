"""Storage contract; providers never decide what belongs in a backup."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BackupInfo:
    name: str
    size: int


class BackupBackend(ABC):
    enabled: bool = True

    @abstractmethod
    def upload(self, archive: Path, name: str) -> None:
        """Store the complete archive atomically, or raise on failure."""

    @abstractmethod
    def download(self, name: str, destination: Path) -> bool:
        """Write an archive to destination; return False only when absent."""

    @abstractmethod
    def list_backups(self) -> list[BackupInfo]:
        """List complete archives available for recovery."""

    @abstractmethod
    def delete(self, name: str) -> None:
        """Delete an archive; an already absent archive is not an error."""


class DummyBackupBackend(BackupBackend):
    """Intentional no-op. No filesystem or network access."""

    enabled = False

    def upload(self, archive: Path, name: str) -> None:
        pass

    def download(self, name: str, destination: Path) -> bool:
        return False

    def list_backups(self) -> list[BackupInfo]:
        return []

    def delete(self, name: str) -> None:
        pass


def create_backend(name: str) -> BackupBackend:
    """Add future providers here; unknown configuration must fail loudly."""
    if name == "dummy":
        return DummyBackupBackend()
    raise ValueError(f"Unknown backup backend: {name}")
