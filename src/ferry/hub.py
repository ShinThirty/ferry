"""The hub's layout on the shared folder:

  <hub>/FORMAT                         "ferry 1" — also the proof the share is mounted
  <hub>/<host>/<repo>/manifest.age     one slot per machine and repo
  <hub>/<host>/<repo>/<stamp>-<pid>-<n>.pack.age
  <hub>/.compacting-<repo>/owner       the one hub-wide lock

Every file is written beside its target as .<name>.part and renamed, so a
reader gets the whole file or none of it.

A share that can't hold extended attributes gets a ._<name> companion from
macOS beside every file and folder (AppleDouble). The client renames and
deletes those along with their file; listings here skip them.

FORMAT and the lock's owner are read by every machine, so they are written as
UTF-8 with LF line ends whatever the OS.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import system
from .crypto import CryptoError, Keyring
from .manifest import Manifest, Pack

FORMAT = "ferry 1"


class HubFormatError(Exception):
    pass


def fsig(path: Path) -> str | None:
    """Size and mtime: a manifest whose signature hasn't moved is the one already
    applied, since a push always replaces it. This keeps a quiet pull to stat calls.
    The mtime is the share's full precision: whole seconds would miss a manifest
    rewritten at the same size within the second, a push that only moves a branch."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return f"{st.st_size} {st.st_mtime_ns}"


def _files(d: Path, pattern: str) -> list[Path]:
    return [f for f in d.glob(pattern) if not f.name.startswith("._")]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


class Hub:
    def __init__(self, root: Path, host: str, keys: Keyring | None):
        self.root = root
        self.host = host
        self.keys = keys
        self._stamp = time.strftime("%Y%m%d-%H%M%S")
        self._seq = 0

    # --- the hub as a whole --------------------------------------------------
    def is_up(self) -> bool:
        """FORMAT is the proof the share is mounted. An unmounted mount point can
        still exist as an empty local directory, and syncing into that would look fine."""
        try:
            fmt = _read(self.root / "FORMAT").rstrip("\n")
        except (FileNotFoundError, NotADirectoryError):
            return False
        if fmt != FORMAT:
            raise HubFormatError(f"the hub at {self.root} is in a format this ferry doesn't know ({fmt})")
        return True

    def create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _write(self.root / "FORMAT", FORMAT + "\n")

    def hosts(self) -> list[str]:
        try:
            return sorted(d.name for d in self.root.iterdir() if d.is_dir() and not d.name.startswith("."))
        except FileNotFoundError:
            return []

    def others(self) -> list[str]:
        return [h for h in self.hosts() if h != self.host]

    def has_host(self, host: str) -> bool:
        return (self.root / host).is_dir()

    def by_recency(self, repo: str) -> list[str]:
        """Machines holding a copy of repo, the latest written first."""
        found = []
        for h in self.hosts():
            try:
                found.append((-(self.manifest_file(h, repo).stat().st_mtime), h))
            except FileNotFoundError:
                pass
        return [h for _, h in sorted(found)]

    # --- slots ---------------------------------------------------------------
    def slot(self, host: str, repo: str) -> Path:
        return self.root / host / repo

    def manifest_file(self, host: str, repo: str) -> Path:
        return self.slot(host, repo) / "manifest.age"

    def read_manifest(self, host: str, repo: str) -> Manifest | None:
        """None if absent or unreadable."""
        f = self.manifest_file(host, repo)
        if not f.is_file():
            return None
        try:
            return Manifest.parse(self.keys.open_text(f))
        except (CryptoError, ValueError):
            return None

    def packs(self, host: str, repo: str) -> set[str]:
        """The pack files in a slot, named in its manifest or not."""
        return {f.name for f in _files(self.slot(host, repo), "*.pack.age")}

    def get_pack(self, host: str, repo: str, name: str, dst: Path) -> bool:
        try:
            self.keys.open_file(self.slot(host, repo) / name, dst)
            return True
        except CryptoError:
            return False

    def put_pack(self, repo: str, bundle: Path, tips: list[str]) -> Pack:
        """Seal a bundle into this machine's slot; the bundle itself is removed."""
        self._seq += 1
        name = f"{self._stamp}-{os.getpid()}-{self._seq}.pack.age"
        target = self.slot(self.host, repo) / name
        part = target.with_name(f".{name}.part")
        self.keys.seal_file(bundle, part)
        system.retry(os.replace, part, target)
        bundle.unlink()
        return Pack(name, target.stat().st_size, tips)

    def put_manifest(self, repo: str, manifest: Manifest) -> None:
        target = self.manifest_file(self.host, repo)
        part = target.with_name(".manifest.age.part")
        self.keys.seal_text(manifest.text(), part)
        system.retry(os.replace, part, target)

    def sweep(self, repo: str, keep: set[str]) -> None:
        """Drop packs the manifest no longer names, and whatever a push that died
        midway left behind. Only ever after the new manifest is in place. A file
        another machine still holds open is left for the next push to drop."""
        slot = self.slot(self.host, repo)
        for f in [slot / name for name in self.packs(self.host, repo) - keep] + _files(slot, ".*.part"):
            try:
                system.retry(lambda: f.unlink(missing_ok=True))
            except OSError:
                pass

    def retire(self, host: str) -> None:
        shutil.rmtree(self.root / host)
        (self.root / f"._{host}").unlink(missing_ok=True)
        for lock in self.root.glob(".compacting-*"):        # a compaction it never finished
            if self._owner(lock) == host:
                shutil.rmtree(lock, ignore_errors=True)

    # --- the compaction lock -------------------------------------------------
    # One compaction at a time. mkdir is atomic on the share's server. Only its
    # owner takes it away, and only once its slot shows what it did — so a
    # compaction that died halfway still holds every other machine off.
    def _lock(self, repo: str) -> Path:
        return self.root / f".compacting-{repo}"

    @staticmethod
    def _owner(lock: Path) -> str:
        try:
            return _read(lock / "owner").strip()
        except (FileNotFoundError, NotADirectoryError):
            return ""

    def lock_held(self, repo: str) -> bool:
        return self._lock(repo).is_dir()

    def lock_owner(self, repo: str) -> str:
        return self._owner(self._lock(repo))

    def take_lock(self, repo: str) -> str | None:
        """None once this machine holds the lock — new, or left from an earlier
        attempt; otherwise the holder's name ("" if unknown)."""
        lock = self._lock(repo)
        try:
            lock.mkdir()
        except FileExistsError:
            owner = self._owner(lock)
            if owner != self.host:
                return owner
        _write(lock / "owner", self.host + "\n")
        return None

    def release_lock(self, repo: str) -> None:
        try:
            system.retry(shutil.rmtree, self._lock(repo))
        except OSError:             # gone already; or still held open, and the next push tries again
            pass
