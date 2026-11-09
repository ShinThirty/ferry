"""ferry.toml: which repos a project has, and where each one lives.

The project folder itself is always the repo "root", and ferry.toml, tracked
in it, names the project and adds the rest:

  name = "notes"

  [repos.lab]                  a folder of the project with a .git of its own
  path = "lab"

  [repos.memory]               Claude Code's memory folder for this project
  claude-memory = true
  auto = true
  union = ["MEMORY.md"]

  [repos.files]
  path = "files"
  auto = true

A repo is kept (the default) or auto:

  kept  its history is the record: ferry never commits in it, never merges two
        machines' work in it and never compacts it. A post-commit hook pushes
        each commit.
  auto  its history is ferry's own snapshots: ferry commits whatever changed,
        merges every machine's changes, and swaps the history for one commit
        once old versions pile up. Its git dir lives in .ferry/, outside its
        folder. union names files whose merges keep both sides' lines.

Root must ignore .ferry/ and every repo folder inside the project.

Root is kept, unless ferry.toml says sync-root = false: then its history
travels by a remote of its own (a repo on GitHub, say), and ferry gives it no
slot on the hub and no hook. The folder is still the project, and root must
still ignore what the other repos hold. A new machine clones root first, and
enroll fills in the rest around it.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import home, valid_name

FILE = "ferry.toml"
STATE = ".ferry"
ROOT = "root"
_REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")       # a folder on the hub, and part of ref names
_KEYS = {"path", "claude-memory", "auto", "union"}


class ProjectError(Exception):
    pass


def find(start: Path) -> Path | None:
    """The project folder start is in: the nearest one up holding ferry.toml."""
    for d in (start, *start.parents):
        if (d / FILE).is_file():
            return d
    return None


def claude_slug(path: str) -> str:
    """The folder Claude Code keeps a project's sessions and memory in, under
    ~/.claude/projects/: each UTF-16 unit of the path that isn't an ASCII letter
    or digit becomes "-" (C:\\Users\\x\\notes: C--Users-x-notes), and past 200
    the name is cut and a hash of the whole path appended."""
    units = memoryview(path.encode("utf-16-le")).cast("H")
    slug = "".join(chr(u) if chr(u).isascii() and chr(u).isalnum() else "-" for u in units)
    if len(slug) <= 200:
        return slug
    h = 0
    for u in units:                                     # JavaScript's (h << 5) - h + c, in 32 bits
        h = (h * 31 + u) & 0xFFFFFFFF
    h = abs(h - (1 << 32) if h >> 31 else h)
    digits = ""
    while True:
        h, r = divmod(h, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + digits
        if not h:
            break
    return f"{slug[:200]}-{digits}"


@dataclass(frozen=True)
class Repo:
    name: str
    auto: bool = False
    path: str = ""                  # inside the project, "/"-separated; "" for root and Claude's memory
    claude_memory: bool = False
    union: tuple[str, ...] = ()


@dataclass(frozen=True)
class Project:
    name: str
    repos: tuple[Repo, ...]         # root first if ferry syncs it, then in ferry.toml's order

    @classmethod
    def bare(cls, name: str) -> Project:
        """Root alone: what a new machine knows before root, and its ferry.toml, arrive."""
        return cls(name, (Repo(ROOT),))

    @classmethod
    def load(cls, folder: Path) -> Project:
        f = folder / FILE
        try:
            data = tomllib.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ProjectError(f"no {FILE} in {folder}") from None
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise ProjectError(f"can't read {f} — {e}") from e
        return cls.parse(data, str(f))

    @classmethod
    def parse(cls, data: dict, where: str = FILE) -> Project:
        def bad(msg: str) -> ProjectError:
            return ProjectError(f"{where}: {msg}")

        if extra := set(data) - {"name", "sync-root", "repos"}:
            raise bad(f"unknown key '{min(extra)}'")
        name = data.get("name")
        if not isinstance(name, str) or not valid_name(name):
            raise bad("name = \"...\" is needed: letters, digits, . _ - (it names this project's folder in ~/.config/ferry)")
        sync_root = data.get("sync-root", True)
        if not isinstance(sync_root, bool):
            raise bad("sync-root is true or false")
        tables = data.get("repos", {})
        if not isinstance(tables, dict):
            raise bad("repos must be tables, [repos.NAME]")
        repos = [Repo(ROOT)] if sync_root else []
        for n, t in tables.items():
            where_n = f"[repos.{n}]"
            if n == ROOT:
                raise bad("root is the project folder itself and needs no [repos.root]")
            if not _REPO_NAME.fullmatch(n):
                raise bad(f"{where_n}: a repo's name is letters, digits, _ and -")
            if not isinstance(t, dict):
                raise bad(f"{where_n} must be a table")
            if extra := set(t) - _KEYS:
                raise bad(f"{where_n}: unknown key '{min(extra)}'")
            auto, memory, union = t.get("auto", False), t.get("claude-memory", False), t.get("union", [])
            if not isinstance(auto, bool) or not isinstance(memory, bool):
                raise bad(f"{where_n}: auto and claude-memory are true or false")
            if not isinstance(union, list) or not all(isinstance(u, str) and u for u in union):
                raise bad(f"{where_n}: union is a list of file names")
            if union and not auto:
                raise bad(f"{where_n}: union is for auto repos — ferry never merges a kept one")
            path = t.get("path")
            if memory == (path is not None):
                raise bad(f"{where_n}: give path = \"...\" or claude-memory = true, one or the other")
            if path is not None:
                parts = path.split("/") if isinstance(path, str) else []
                if not parts or any(p in ("", ".", "..") or "\\" in p for p in parts) or parts[0] in (STATE, ".git"):
                    raise bad(f"{where_n}: path is a folder inside the project, written a/b")
                path = "/".join(parts)
            repos.append(Repo(n, auto, path or "", memory, tuple(union)))

        if not repos:
            raise bad("sync-root = false and no [repos.NAME] leaves ferry nothing to sync")
        if sum(r.claude_memory for r in repos) > 1:
            raise bad("only one repo can be Claude's memory folder")
        paths = [r.path for r in repos if r.path]
        for i, p in enumerate(paths):
            for q in paths[i + 1:]:
                if p == q or q.startswith(p + "/") or p.startswith(q + "/"):
                    raise bad(f"{p} and {q} overlap — each repo needs a folder of its own")
        return cls(name, tuple(repos))

    @property
    def names(self) -> list[str]:
        return [r.name for r in self.repos]

    @property
    def auto(self) -> list[str]:
        return [r.name for r in self.repos if r.auto]

    @property
    def kept(self) -> list[str]:
        return [r.name for r in self.repos if not r.auto]

    @property
    def syncs_root(self) -> bool:
        return ROOT in self.names

    def repo(self, name: str) -> Repo:
        for r in self.repos:
            if r.name == name:
                return r
        raise KeyError(name)


@dataclass(frozen=True)
class Layout:
    """Where a project's repos live on this machine."""
    folder: Path
    project: Project

    @property
    def state(self) -> Path:
        return self.folder / STATE

    def worktree(self, name: str) -> Path:
        if name == ROOT:                    # synced or not: what the other repos sit in
            return self.folder
        r = self.project.repo(name)
        if r.claude_memory:
            return home() / ".claude" / "projects" / claude_slug(str(self.folder)) / "memory"
        return self.folder.joinpath(*r.path.split("/"))

    def gitdir(self, name: str) -> Path:
        """The auto repos keep theirs in .ferry/, outside their work trees, so no
        .git ever lands inside Claude's memory folder or a folder of plain files."""
        if name == ROOT:
            return self.folder / ".git"
        return self.state / f"{name}.git" if self.project.repo(name).auto else self.worktree(name) / ".git"

    @property
    def hub_file(self) -> Path:
        """The hub's folder as this machine mounts it: set once, by init or
        enroll, and again by ferry hub when that changes."""
        return self.state / "hub"

    def hooks(self, name: str) -> Path:
        """The hook folder a kept repo's core.hooksPath names."""
        return self.state / "hooks" / name
