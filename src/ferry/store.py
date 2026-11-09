"""Where a hub's files are kept: the storage under hub, which knows only files
and folders, never what they mean.

hub names each file by a key, its path under the hub with / between the parts:
FORMAT, alpha/source/manifest.age. The key "" is the hub itself. A store hands
the hub whole files only, and lists only those:

  reading   a local path holding a file's bytes
  writing   a local path to fill; the file appears whole once it's done, or not at all
  claim     a file made only if no machine has made it yet: the compaction lock

A store that can't reach the hub raises ConnectionError, which hub takes for
the hub being down.

FolderStore keeps the files in a folder on a shared drive, and S3Store (in s3)
in an S3 bucket. On a share, every file is written beside its target as
.<name>.part and renamed, so a reader gets the whole file or none of it. A
share that can't hold extended attributes gets a ._<name> companion from macOS
beside every file and folder (AppleDouble). The client renames and deletes
those along with their file; listings here skip them.
"""
from __future__ import annotations

import errno
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import system


class StoreError(Exception):
    pass


@dataclass(frozen=True)
class Stat:
    sig: str            # changes whenever the file is replaced
    mtime: float        # when it was last written, to order files by


class Store(ABC):
    """A hub's files, wherever they are kept. location is what this machine
    calls the hub: what .ferry/hub holds, and what ferry says. mounted is
    whether it is reached through a mount, whose path differs from machine to
    machine. missing_hint is what to check when no hub is found there."""

    location: str
    mounted: bool
    missing_hint: str

    @abstractmethod
    def check_vacant(self) -> None:
        """StoreError unless a new hub can go here: there is somewhere to make it,
        and nothing there yet."""

    @abstractmethod
    def mkdir(self, key: str) -> None:
        """Folder key, and those above it, if they aren't there yet."""

    @abstractmethod
    def is_folder(self, key: str) -> bool:
        ...

    @abstractmethod
    def folders(self, key: str) -> set[str]:
        """The names of the folders directly in folder key; none if it isn't there."""

    @abstractmethod
    def files(self, key: str) -> set[str]:
        """The names of the whole files directly in folder key; none if it isn't there."""

    @abstractmethod
    def stat(self, key: str) -> Stat | None:
        """None if file key isn't there."""

    @abstractmethod
    def reading(self, key: str) -> AbstractContextManager[Path]:
        """A local path holding file key, for the length of the with block.
        FileNotFoundError if it isn't there."""

    @abstractmethod
    def writing(self, key: str) -> AbstractContextManager[Path]:
        """A local path to fill, in a folder already made. Once the with block ends
        without an error, file key holds it; until then, or if it ends with one,
        every reader sees key as it was."""

    @abstractmethod
    def claim(self, key: str, data: bytes) -> bool:
        """Make file key holding data, unless it was made already, by this machine
        or any other. False if it was. key's folder holds key alone, and goes with
        it (delete_folder)."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """File key, if it's there."""

    @abstractmethod
    def delete_folder(self, key: str) -> None:
        """Folder key and everything in it, if it's there."""

    @abstractmethod
    def clean(self, key: str) -> None:
        """What writes into folder key left behind when they died midway. What
        can't go now is left for the next time."""


def store_at(location: str) -> Store:
    """The store a hub's location names: s3://BUCKET/PREFIX, or a folder."""
    if location.startswith("s3://"):
        try:
            from .s3 import S3Store                 # boto3 is an extra, loaded only for a hub in S3
        except ModuleNotFoundError as e:
            if (e.name or "").split(".")[0] not in ("boto3", "botocore", "s3transfer"):
                raise
            raise StoreError("a hub in S3 needs boto3, which comes with ferry's s3 extra: "
                             "uv tool install 'ferry[s3] @ git+https://github.com/ShinThirty/ferry'") from e
        return S3Store.at(location)
    return FolderStore(Path(location).absolute())


def _partial(name: str) -> bool:
    return name.startswith(".") and name.endswith(".part")


class FolderStore(Store):
    """A folder on a shared drive, as this machine mounts it."""

    mounted = True
    missing_hint = "is the share mounted, and is that the hub's folder on it?"

    def __init__(self, root: Path):
        self.root = root
        self.location = str(root)

    def path(self, key: str) -> Path:
        return self.root / key

    def _entries(self, key: str) -> list[os.DirEntry]:
        try:
            with os.scandir(self.path(key)) as it:
                return [e for e in it if not e.name.startswith("._")]
        except (FileNotFoundError, NotADirectoryError):
            return []

    def check_vacant(self) -> None:
        if not self.root.parent.is_dir():
            raise StoreError(f"{self.root.parent} is not there — is the share mounted?")
        if self.root.is_dir() and any(self.root.iterdir()):
            raise StoreError(f"{self.root} isn't empty — a hub starts in an empty folder")

    def mkdir(self, key: str) -> None:
        self.path(key).mkdir(parents=True, exist_ok=True)

    def is_folder(self, key: str) -> bool:
        return self.path(key).is_dir()

    def folders(self, key: str) -> set[str]:
        return {e.name for e in self._entries(key) if e.is_dir()}

    def files(self, key: str) -> set[str]:
        return {e.name for e in self._entries(key) if e.is_file() and not _partial(e.name)}

    def stat(self, key: str) -> Stat | None:
        """The mtime is the share's full precision: whole seconds would miss a
        manifest rewritten at the same size within the second, a push that only
        moves a branch."""
        try:
            st = self.path(key).stat()
        except FileNotFoundError:
            return None
        return Stat(f"{st.st_size} {st.st_mtime_ns}", st.st_mtime)

    @contextmanager
    def reading(self, key: str) -> Iterator[Path]:
        path = self.path(key)
        if not path.is_file():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        yield path

    @contextmanager
    def writing(self, key: str) -> Iterator[Path]:
        target = self.path(key)
        part = target.with_name(f".{target.name}.part")
        yield part
        system.retry(os.replace, part, target)

    def claim(self, key: str, data: bytes) -> bool:
        """mkdir is atomic on the share's server, so what is claimed is key's
        folder, and key is written into it once that's made. A folder without key
        is a claim by a machine that died before it could say which."""
        path = self.path(key)
        try:
            path.parent.mkdir()
        except FileExistsError:
            return False
        path.write_bytes(data)
        return True

    def delete(self, key: str) -> None:
        path = self.path(key)
        system.retry(lambda: path.unlink(missing_ok=True))

    def delete_folder(self, key: str) -> None:
        """With the ._ companion beside it."""
        path = self.path(key)
        try:
            system.retry(shutil.rmtree, path)
        except FileNotFoundError:
            pass
        (path.parent / f"._{path.name}").unlink(missing_ok=True)

    def clean(self, key: str) -> None:
        """A file another machine still holds open is left for the next time."""
        for e in self._entries(key):
            if _partial(e.name):
                try:
                    system.retry(os.unlink, e.path)
                except OSError:
                    pass
