"""The one place ferry uses age.

Everything on the hub is age ciphertext sealed to one key. pyrage implements
the age format itself, so no age tool is needed, and the age CLI can still open
any of it by hand: the key file is in age-keygen's format.
"""
from __future__ import annotations

import datetime
import os
from collections.abc import Sequence
from pathlib import Path

import pyrage
from pyrage import x25519


class CryptoError(Exception):
    pass


def _identities(path: Path) -> list[x25519.Identity]:
    try:
        return [x25519.Identity.from_str(line.strip()) for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("AGE-SECRET-KEY-")]
    except (OSError, pyrage.IdentityError) as e:
        raise CryptoError(f"can't read the key at {path} — {e}") from e


def generate_key(path: Path) -> None:
    """A new key, written as age-keygen writes one and readable by this user only
    (on Windows the mode does nothing: the profile folder's ACL does that)."""
    key = x25519.Identity.generate()
    created = datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# created: {created}\n# public key: {key.to_public()}\n{key}\n")


class Keyring:
    """The current key, which seals and opens, and any old ones a rekey kept, which only open."""

    def __init__(self, key: Path, old: Sequence[Path] = ()):
        current = _identities(key)
        if not current:
            raise CryptoError(f"no age key in {key}")
        self.recipient = str(current[0].to_public())
        self._seal_to = [current[0].to_public()]
        self._open_with = current + [i for p in old for i in _identities(p)]

    @classmethod
    def load(cls, conf_dir: Path) -> Keyring:
        return cls(conf_dir / "identity", sorted(conf_dir.glob("identity.old-*")))

    def seal_file(self, src: Path, dst: Path) -> None:
        try:
            pyrage.encrypt_file(str(src), str(dst), self._seal_to)
        except (pyrage.EncryptError, OSError) as e:
            raise CryptoError(f"can't write {dst} — {e}") from e

    def seal_text(self, text: str, dst: Path) -> None:
        try:
            dst.write_bytes(pyrage.encrypt(text.encode(), self._seal_to))
        except (pyrage.EncryptError, OSError) as e:
            raise CryptoError(f"can't write {dst} — {e}") from e

    def open_file(self, src: Path, dst: Path) -> None:
        try:
            pyrage.decrypt_file(str(src), str(dst), self._open_with)
        except (pyrage.DecryptError, OSError) as e:
            raise CryptoError(f"can't read {src} — {e}") from e

    def open_text(self, src: Path) -> str:
        try:
            return pyrage.decrypt(src.read_bytes(), self._open_with).decode()
        except (pyrage.DecryptError, OSError, UnicodeDecodeError) as e:
            raise CryptoError(f"can't read {src} — {e}") from e
