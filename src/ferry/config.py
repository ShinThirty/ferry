"""This machine's side of a project: which machine it is, which folder it
syncs, and the key — kept in ~/.config/ferry/<project>/, outside the folder,
so a copy of the folder can't pass itself off as the original. The hub's
folder, as this machine mounts it, is the folder's own: .ferry/hub."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def home() -> Path:
    """$HOME where it is set, as the tests' fake machines set it; else the OS's
    idea of it (on Windows, outside Git Bash, there is no $HOME)."""
    return Path(os.environ["HOME"]) if os.environ.get("HOME") else Path.home()


def valid_name(name: str) -> bool:
    """A machine or project name: it becomes a folder on the hub or in ~/.config."""
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", name)) and not name.startswith(".")


@dataclass(frozen=True)
class Settings:
    maxchain: int = 24           # packs in a chain before it restarts from a new base
    foreground_mb: int = 32      # the most a session start waits to download
    compact_mb: int = 64         # old versions an auto repo carries before it compacts
    keep_days: int = 14          # how long a machine keeps the history a compaction replaced

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(int(env.get("FERRY_MAXCHAIN", cls.maxchain)),
                   int(env.get("FERRY_FOREGROUND_MB", cls.foreground_mb)),
                   int(env.get("FERRY_COMPACT_MB", cls.compact_mb)),
                   int(env.get("FERRY_KEEP_DAYS", cls.keep_days)))


@dataclass(frozen=True)
class Machine:
    """~/.config/ferry/<project>/: config, the key, and any old keys a rekey kept."""
    dir: Path

    @classmethod
    def of(cls, project: str) -> Machine:
        base = os.environ.get("XDG_CONFIG_HOME") or home() / ".config"
        return cls(Path(base) / "ferry" / project)

    @property
    def config(self) -> Path:
        return self.dir / "config"

    @property
    def key(self) -> Path:
        return self.dir / "identity"


@dataclass(frozen=True)
class Config:
    """<machine dir>/config: which machine this is, and which folder it syncs."""
    host: str
    repo: Path

    @classmethod
    def read(cls, path: Path) -> Config | None:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        kv = dict(line.partition("=")[::2] for line in text.splitlines())
        return cls(kv.get("host", ""), Path(kv.get("repo", "")))

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"host={self.host}\nrepo={self.repo}\n", encoding="utf-8", newline="\n")
