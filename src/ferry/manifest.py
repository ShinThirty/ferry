"""A slot's manifest, and an auto repo's epoch record: parsing and formatting only.

A manifest is the decrypted text of <hub>/<host>/<repo>/manifest.age:

  ferry manifest 1
  recipient age1…                  the key every pack in the chain is sealed to
  head master                      the branch a new machine checks out
  epoch <n> <root> <tree> <time>   once an auto repo has compacted (see below)
  cover <host> <sha>
  ref <sha> refs/heads/…           where this machine's branches are
  pack <file> <bytes> <sha>…       the chain, base first; each with the branch
                                   tips it completes

The epoch and cover lines describe the compaction a repo's history starts at:

  epoch <n> <root> <tree> <time>   the root commit. A machine that already
                                   holds <tree> builds it, byte for byte,
                                   instead of downloading it
  cover <host> <sha>               each machine's branch as the compaction
                                   found it — work <tree> already holds

The same lines, alone, are a repo's epoch record: <gitdir>/sync-epoch for this
machine's, and .ferry/seen/<host>.<repo>.epoch for the last one seen from each
other machine. Every machine reads this text, so its format is fixed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import system


MAGIC = "ferry manifest 1"


@dataclass(frozen=True)
class Epoch:
    number: int
    root: str
    tree: str
    time: int

    def line(self) -> str:
        return f"epoch {self.number} {self.root} {self.tree} {self.time}"


@dataclass
class EpochRecord:
    epoch: Epoch | None = None
    covers: list[tuple[str, str]] = field(default_factory=list)      # (host, sha)

    @property
    def number(self) -> int:
        return self.epoch.number if self.epoch else 0

    def lines(self) -> list[str]:
        head = [self.epoch.line()] if self.epoch else []
        return head + [f"cover {h} {sha}" for h, sha in self.covers]

    @classmethod
    def parse(cls, lines: list[str]) -> EpochRecord:
        rec = cls()
        for line in lines:
            f = line.split()
            if f[:1] == ["epoch"] and len(f) >= 5 and rec.epoch is None:
                rec.epoch = Epoch(int(f[1]), f[2], f[3], int(f[4]))
            elif f[:1] == ["cover"] and len(f) >= 3:
                rec.covers.append((f[1], f[2]))
        return rec

    @classmethod
    def load(cls, path: Path) -> EpochRecord:
        try:
            return cls.parse(path.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            return cls()

    def save(self, path: Path) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(f"{line}\n" for line in self.lines()), encoding="utf-8", newline="\n")
        system.retry(os.replace, tmp, path)


@dataclass
class Pack:
    name: str
    size: int
    tips: list[str]

    def line(self) -> str:
        return f"pack {self.name} {self.size} {' '.join(self.tips)}"


@dataclass
class Manifest:
    recipient: str
    head: str
    epoch: EpochRecord
    refs: list[tuple[str, str]]       # (sha, ref name), as for-each-ref lists them
    packs: list[Pack]

    @property
    def published(self) -> list[str]:
        """The branch tips this slot holds."""
        return [sha for sha, _ in self.refs]

    def branch(self, name: str) -> str | None:
        return next((sha for sha, ref in self.refs if ref == f"refs/heads/{name}"), None)

    def text(self) -> str:
        lines = [MAGIC, f"recipient {self.recipient}", f"head {self.head}", *self.epoch.lines(),
                 *(f"ref {sha} {ref}" for sha, ref in self.refs), *(p.line() for p in self.packs)]
        return "".join(f"{line}\n" for line in lines)

    @classmethod
    def parse(cls, text: str) -> Manifest:
        lines = text.split("\n")
        if lines[0] != MAGIC:
            raise ValueError("not a manifest this ferry knows")
        m = cls(recipient="", head="", epoch=EpochRecord.parse(lines), refs=[], packs=[])
        for line in lines[1:]:
            key, _, rest = line.partition(" ")
            if key == "recipient":
                m.recipient = rest
            elif key == "head":
                m.head = rest
            elif key == "ref":
                sha, _, ref = rest.partition(" ")
                m.refs.append((sha, ref))
            elif key == "pack":
                name, size, *tips = rest.split()
                m.packs.append(Pack(name, int(size), tips))
        return m
