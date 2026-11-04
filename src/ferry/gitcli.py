"""The one place ferry runs git.

Features call these methods, named for what they mean, and never build a git
command line. Revisions are passed as data: a sha, a ref name, or one of the
selectors below.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

MIN_VERSION = (2, 29)               # fetch --no-write-fetch-head
BRANCHES = "--branches"             # every local branch, as a revision selector


def glob(pattern: str) -> str:
    """Every ref matching pattern, as a revision selector."""
    return f"--glob={pattern}"


# Git runs hooks with GIT_DIR and friends pointing at the repo that fired them.
# Inherited, they would aim every call here at that one repo.
_INHERITED = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX",
              "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR")


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _INHERITED}
    env.update(extra or {})
    return env


class GitError(Exception):
    def __init__(self, command: str, code: int, stderr: str):
        lines = stderr.strip().splitlines()
        self.reason = lines[0] if lines else f"exit {code}"     # git's first line, for a warning
        super().__init__(f"git {command} failed — {self.reason}")


def _run(argv: Sequence[str], *, command: str | None = None, input: str | None = None,
         env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Bytes through the pipes, decoded here: a text-mode pipe on Windows sends each
    # \n of input as \r\n, and git update-ref --stdin takes the \r for part of the line.
    stdin = {"input": input.encode("utf-8", "surrogateescape")} if input is not None else {"stdin": subprocess.DEVNULL}
    p = subprocess.run(["git", *argv], env=_env(env), capture_output=True, **stdin)
    out, err = (b.decode("utf-8", "surrogateescape") for b in (p.stdout, p.stderr))
    if check and p.returncode:
        raise GitError(command or argv[0], p.returncode, err)
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


def version() -> tuple[int, int] | None:
    """The installed git's version, or None if there is no git."""
    try:
        out = _run(["--version"]).stdout
    except FileNotFoundError:
        return None
    m = re.match(r"git version (\d+)\.(\d+)", out)
    return (int(m[1]), int(m[2])) if m else None


def init(path: Path, *, bare: bool = False) -> None:
    _run(["init", "-q", *(["--bare"] if bare else []), str(path)])


def _not(exclude: Iterable[str]) -> list[str]:
    exclude = list(exclude)
    return ["--not", *exclude] if exclude else []


class Git:
    """One repo: its git dir, and the work tree its files live in."""

    def __init__(self, git_dir: Path, work_tree: Path):
        self.git_dir = git_dir
        self.work_tree = work_tree

    def _argv(self, args: Sequence[str]) -> list[str]:
        return ["-C", str(self.work_tree), f"--git-dir={self.git_dir}",
                f"--work-tree={self.work_tree}", *args]

    def _run(self, *args: str, **kw) -> subprocess.CompletedProcess[str]:
        return _run(self._argv(args), command=args[0], **kw)

    def _out(self, *args: str, **kw) -> str:
        return self._run(*args, **kw).stdout.strip()

    def _ok(self, *args: str) -> bool:
        return self._run(*args, check=False).returncode == 0

    def _maybe(self, *args: str) -> str | None:
        p = self._run(*args, check=False)
        if p.returncode:
            return None
        return p.stdout.strip() or None

    # --- configuration -------------------------------------------------------
    def ensure_config(self, wanted: dict[str, str]) -> None:
        """Set each key whose effective value differs."""
        have: dict[str, str] = {}
        for line in self._run("config", "--list", check=False).stdout.splitlines():
            k, _, v = line.partition("=")
            have[k.lower()] = v
        for k, v in wanted.items():
            if have.get(k.lower()) != v:
                self._run("config", k, v)

    # --- refs ----------------------------------------------------------------
    def branch(self) -> str | None:
        """The checked-out branch; None when HEAD is detached."""
        return self._maybe("symbolic-ref", "-q", "--short", "HEAD")

    def check_out(self, branch: str) -> None:
        """Point HEAD at a branch, leaving the index and files alone."""
        self._run("symbolic-ref", "HEAD", f"refs/heads/{branch}")

    def resolve(self, rev: str) -> str | None:
        return self._maybe("rev-parse", "-q", "--verify", rev)

    def short(self, rev: str = "HEAD") -> str | None:
        return self._maybe("rev-parse", "--short", rev)

    def tree(self, rev: str = "HEAD") -> str:
        return self._out("rev-parse", f"{rev}^{{tree}}")

    def refs(self, *prefixes: str) -> dict[str, str]:
        """{ref name: sha} under the prefixes, sorted by name."""
        out = self._run("for-each-ref", "--format=%(objectname) %(refname)", *prefixes).stdout
        return {ref: sha for sha, ref in (line.split(" ", 1) for line in out.splitlines())}

    def branches(self) -> dict[str, str]:
        return self.refs("refs/heads/")

    def set_ref(self, ref: str, sha: str, *, expect: str | None = None, reason: str | None = None) -> None:
        self._run("update-ref", *(["-m", reason] if reason else []), ref, sha, *([expect] if expect else []))

    def delete_ref(self, ref: str) -> None:
        self._run("update-ref", "-d", ref, check=False)

    def update_refs(self, changes: dict[str, str | None]) -> None:
        """Set (sha) or delete (None) several refs in one transaction."""
        if changes:
            self._run("update-ref", "--stdin", input="".join(
                f"update {r} {s}\n" if s else f"delete {r}\n" for r, s in changes.items()))

    def delete_refs(self, *prefixes: str) -> None:
        self.update_refs(dict.fromkeys(self.refs(*prefixes)))

    # --- history -------------------------------------------------------------
    def has_commit(self, rev: str) -> bool:
        return self._ok("cat-file", "-e", f"{rev}^{{commit}}")

    def has_tree(self, rev: str) -> bool:
        return self._ok("cat-file", "-e", f"{rev}^{{tree}}")

    def is_ancestor(self, a: str, b: str) -> bool:
        return self._ok("merge-base", "--is-ancestor", a, b)

    def merge_base(self, revs: Iterable[str]) -> str | None:
        return self._maybe("merge-base", "--octopus", *revs)

    def count(self, include: Iterable[str], exclude: Iterable[str] = ()) -> int:
        """Commits reachable from include and not from exclude."""
        return int(self._out("rev-list", "--count", *include, *_not(exclude)))

    def any(self, include: Iterable[str], exclude: Iterable[str] = ()) -> bool:
        p = self._run("rev-list", "-n1", *include, *_not(exclude), check=False)
        return p.returncode == 0 and bool(p.stdout.strip())

    def reaches(self, commits: Sequence[str]) -> bool:
        """Whether local refs already reach every one of these commits — and so,
        since refs only move after git has checked connectivity, all their history."""
        if not commits:
            return True
        p = self._run("rev-list", "-n1", *commits, "--not", "--all", check=False)
        return p.returncode == 0 and not p.stdout.strip()

    def ahead_behind(self, a: str, b: str) -> tuple[int, int]:
        left, right = self._out("rev-list", "--left-right", "--count", f"{a}...{b}").split()
        return int(left), int(right)

    def file_bytes(self) -> int:
        """Packed size of the files HEAD holds."""
        return self._blob_bytes("HEAD^{tree}")

    def history_bytes(self) -> int:
        """Packed size of every file version any branch reaches."""
        return self._blob_bytes(BRANCHES)

    def _blob_bytes(self, *revs: str) -> int:
        lister = subprocess.Popen(["git", *self._argv(["rev-list", "--objects", "--no-object-names", *revs])],
                                  env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        sizer = subprocess.Popen(["git", *self._argv(["cat-file", "--batch-check=%(objecttype) %(objectsize:disk)"])],
                                 env=_env(), stdin=lister.stdout, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
        lister.stdout.close()
        total = 0
        for line in sizer.stdout:
            kind, _, size = line.partition(" ")
            if kind == "blob":
                total += int(size)
        sizer.wait()
        lister.wait()
        return total

    # --- the work tree -------------------------------------------------------
    def changes(self) -> list[str]:
        """Uncommitted changes, one line per path."""
        return self._run("status", "--porcelain").stdout.splitlines()

    def stage_all(self) -> None:
        self._run("add", "-A")

    def has_staged(self) -> bool:
        return not self._ok("diff", "--cached", "--quiet")

    def commit(self, message: str, *, allow_empty: bool = False) -> None:
        self._run("commit", "-q", *(["--allow-empty"] if allow_empty else []), "-m", message)

    def ignores(self, folder: str) -> bool:
        """Whether this repo leaves folder out: ignored, and nothing in it tracked.
        The trailing / says it's a folder, which git can't tell of one not there yet."""
        folder = folder.rstrip("/") + "/"
        return self._ok("check-ignore", "-q", folder) and not self._run("ls-files", "--", folder).stdout

    def tracked_count(self) -> int:
        return len(self._run("ls-files").stdout.splitlines())

    def fast_forward(self, rev: str) -> bool:
        return self._ok("merge", "-q", "--ff-only", rev)

    def merge(self, rev: str, message: str) -> bool:
        """A real merge. On a conflict it is aborted, leaving everything as it was."""
        if self._ok("merge", "-q", "--no-edit", "-m", message, rev):
            return True
        self._run("merge", "--abort", check=False)
        return False

    def reset_keep(self, rev: str) -> None:
        """Move the branch to rev, keeping uncommitted changes; refuses if they'd be lost."""
        self._run("reset", "-q", "--keep", rev)

    def reset_hard(self) -> None:
        self._run("reset", "-q", "--hard")

    def commit_tree(self, tree: str, parents: Iterable[str], message: str, *,
                    author: tuple[str, str] | None = None, date: str | None = None) -> str:
        """A commit object for tree, touching no ref. author is (name, email) and
        stands for the committer too; both default to the repo's config. Given
        both, the same bytes on every machine: neither signing nor a configured
        commit encoding (which would add a header) gets in."""
        env = {}
        if author:
            env |= {"GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
                    "GIT_COMMITTER_NAME": author[0], "GIT_COMMITTER_EMAIL": author[1]}
        if date:
            env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        parent_args = [a for p in parents for a in ("-p", p)]
        argv = self._argv(["commit-tree", "--no-gpg-sign", *parent_args, "-m", message, tree])
        return _run(["-c", "i18n.commitEncoding=UTF-8", *argv], command="commit-tree", env=env).stdout.strip()

    # --- between machines ----------------------------------------------------
    def bundle(self, path: Path, include: Iterable[str], exclude: Iterable[str] = ()) -> None:
        self._run("bundle", "create", "-q", str(path), *include, *_not(exclude))

    def fetch_bundle(self, path: Path, refspec: str) -> None:
        self._run("fetch", "-q", "--no-tags", "--no-write-fetch-head", str(path), refspec)

    # --- housekeeping --------------------------------------------------------
    def drop_unreachable(self) -> None:
        """Forget the reflog's hold on old history, then delete what nothing reaches."""
        self._run("reflog", "expire", "--expire-unreachable=now", "--all")
        self._run("gc", "--quiet", "--prune=now")

    def interactive(self, args: Sequence[str]) -> int:
        """Run git as typed, at the terminal; for finishing a merge by hand."""
        return subprocess.run(["git", *self._argv(args)], env=_env()).returncode
