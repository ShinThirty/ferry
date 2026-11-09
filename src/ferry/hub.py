"""The hub's layout, kept in a store (see store):

  <hub>/FORMAT                         "ferry 1" — also the proof the share is mounted
  <hub>/<host>/<repo>/manifest.age     one slot per machine and repo
  <hub>/<host>/<repo>/<stamp>-<pid>-<n>.pack.age
  <hub>/.compacting-<repo>/owner       the one hub-wide lock

The store hands over whole files only, so a reader gets a whole manifest or
pack or none of it.

FORMAT and the lock's owner are read by every machine, so they are written as
UTF-8 with LF line ends whatever the OS.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .crypto import CryptoError, Keyring
from .manifest import Manifest, Pack
from .store import Store

FORMAT = "ferry 1"


class HubFormatError(Exception):
    pass


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


class Hub:
    def __init__(self, store: Store, host: str, keys: Keyring | None):
        self.store = store
        self.host = host
        self.keys = keys
        self._stamp = time.strftime("%Y%m%d-%H%M%S")
        self._seq = 0

    @property
    def location(self) -> str:
        return self.store.location

    def _text(self, key: str) -> str | None:
        try:
            with self.store.reading(key) as f:
                return f.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    # --- the hub as a whole --------------------------------------------------
    def is_up(self) -> bool:
        """FORMAT is the proof the share is mounted. An unmounted mount point can
        still exist as an empty local directory, and syncing into that would look fine."""
        fmt = self._text("FORMAT")
        if fmt is None:
            return False
        fmt = fmt.rstrip("\n")
        if fmt != FORMAT:
            raise HubFormatError(f"the hub at {self.location} is in a format this ferry doesn't know ({fmt})")
        return True

    def create(self) -> None:
        self.store.mkdir("")
        with self.store.writing("FORMAT") as f:
            _write(f, FORMAT + "\n")

    def hosts(self) -> list[str]:
        return sorted(h for h in self.store.folders("") if not h.startswith("."))

    def others(self) -> list[str]:
        return [h for h in self.hosts() if h != self.host]

    def has_host(self, host: str) -> bool:
        return self.store.is_folder(host)

    def join(self) -> None:
        """This machine's place on the hub, made before anything goes in it."""
        self.store.mkdir(self.host)

    def by_recency(self, repo: str) -> list[str]:
        """Machines holding a copy of repo, the latest written first."""
        found = []
        for h in self.hosts():
            if st := self.store.stat(self._manifest(h, repo)):
                found.append((-st.mtime, h))
        return [h for _, h in sorted(found)]

    # --- slots ---------------------------------------------------------------
    @staticmethod
    def _slot(host: str, repo: str) -> str:
        return f"{host}/{repo}"

    def _manifest(self, host: str, repo: str) -> str:
        return f"{self._slot(host, repo)}/manifest.age"

    def make_slot(self, repo: str) -> None:
        self.store.mkdir(self._slot(self.host, repo))

    def manifest_sig(self, host: str, repo: str) -> str | None:
        """A manifest whose signature hasn't moved is the one already applied, since
        a push always replaces it. This keeps a quiet pull to stat calls. None if
        the slot has no manifest."""
        st = self.store.stat(self._manifest(host, repo))
        return st.sig if st else None

    def has_manifest(self, host: str, repo: str) -> bool:
        return self.manifest_sig(host, repo) is not None

    def read_manifest(self, host: str, repo: str) -> Manifest | None:
        """None if absent or unreadable."""
        try:
            with self.store.reading(self._manifest(host, repo)) as f:
                return Manifest.parse(self.keys.open_text(f))
        except (FileNotFoundError, CryptoError, ValueError):
            return None

    def packs(self, host: str, repo: str) -> set[str]:
        """The pack files in a slot, named in its manifest or not."""
        return {f for f in self.store.files(self._slot(host, repo)) if f.endswith(".pack.age")}

    def get_pack(self, host: str, repo: str, name: str, dst: Path) -> bool:
        try:
            with self.store.reading(f"{self._slot(host, repo)}/{name}") as f:
                self.keys.open_file(f, dst)
            return True
        except (FileNotFoundError, CryptoError):
            return False

    def put_pack(self, repo: str, bundle: Path, tips: list[str]) -> Pack:
        """Seal a bundle into this machine's slot; the bundle itself is removed."""
        self._seq += 1
        name = f"{self._stamp}-{os.getpid()}-{self._seq}.pack.age"
        with self.store.writing(f"{self._slot(self.host, repo)}/{name}") as f:
            self.keys.seal_file(bundle, f)
            size = f.stat().st_size
        bundle.unlink()
        return Pack(name, size, tips)

    def put_manifest(self, repo: str, manifest: Manifest) -> None:
        with self.store.writing(self._manifest(self.host, repo)) as f:
            self.keys.seal_text(manifest.text(), f)

    def sweep(self, repo: str, keep: set[str]) -> None:
        """Drop packs the manifest no longer names, and whatever a push that died
        midway left behind. Only ever after the new manifest is in place. A file
        another machine still holds open is left for the next push to drop."""
        slot = self._slot(self.host, repo)
        for name in self.packs(self.host, repo) - keep:
            try:
                self.store.delete(f"{slot}/{name}")
            except OSError:
                pass
        self.store.clean(slot)

    def retire(self, host: str) -> None:
        self.store.delete_folder(host)
        for lock in self.store.folders(""):
            if lock.startswith(".compacting-") and self._owner(lock) == host:   # a compaction it never finished
                self._unlock(lock)

    # --- the compaction lock -------------------------------------------------
    # One compaction at a time: the store's claim lets one machine in, however
    # many try. Only its owner takes it away, and only once its slot shows what
    # it did — so a compaction that died halfway still holds every other machine off.
    @staticmethod
    def _lock(repo: str) -> str:
        return f".compacting-{repo}"

    def _owner(self, lock: str) -> str:
        return (self._text(f"{lock}/owner") or "").strip()

    def _unlock(self, lock: str) -> None:
        try:
            self.store.delete_folder(lock)
        except OSError:             # still held open, and the next push tries again
            pass

    def lock_held(self, repo: str) -> bool:
        return self.store.is_folder(self._lock(repo))

    def lock_owner(self, repo: str) -> str:
        return self._owner(self._lock(repo))

    def take_lock(self, repo: str) -> str | None:
        """None once this machine holds the lock — new, or left from an earlier
        attempt; otherwise the holder's name ("" if unknown)."""
        lock = self._lock(repo)
        if not self.store.claim(f"{lock}/owner", f"{self.host}\n".encode()):
            owner = self._owner(lock)
            if owner != self.host:
                return owner
        return None

    def release_lock(self, repo: str) -> None:
        self._unlock(self._lock(repo))
