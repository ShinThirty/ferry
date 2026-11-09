"""The features: pull, fetch, adopt, compact, push.

Each method here says what happens to a repo; how git, age and the share do it
is in gitcli, crypto and hub, and the choices with no I/O in them are in plan.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import gitcli, plan, system
from .config import Settings
from .crypto import Keyring
from .gitcli import BRANCHES, Git, GitError
from .hub import Hub
from .manifest import Epoch, EpochRecord, Manifest, Pack
from .project import ROOT, STATE, Layout
from .ui import BG, HOOK, UI, human

# The author of the commit a compaction starts from: every machine that builds
# it must spell it alike to get the same bytes.
EPOCH_AUTHOR = ("ferry", "ferry@epoch")

# What a kept repo's core.hooksPath runs after each commit. With no ferry on the
# PATH it does nothing, and the next run sends the commit.
POST_COMMIT = """#!/bin/sh
# Written by ferry, which rewrites it on every run: publish the commit just
# made to the hub, detached, so the commit never waits on the share.
command -v ferry >/dev/null 2>&1 || exit 0
exec ferry push --only {repo} --background
"""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except FileNotFoundError:
        return ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def unignored(layout: Layout) -> list[str]:
    """Folders root must leave out but doesn't: .ferry/, and each repo's own
    inside the project. Root would otherwise carry their files in its history."""
    root = Git(layout.gitdir(ROOT), layout.folder)
    return [p for p in [STATE, *(r.path for r in layout.project.repos if r.path)] if not root.ignores(p)]


class Sync:
    def __init__(self, layout: Layout, hub: Hub, keys: Keyring, ui: UI, settings: Settings):
        self.layout = layout
        self.project = layout.project
        self.hub = hub
        self.keys = keys
        self.ui = ui
        self.settings = settings
        self.host = hub.host
        self.state = layout.state
        self.force = False                 # compact whatever the size
        self.compacted = ""                # what the last compaction dropped
        self.budget = plan.Budget()
        self._lock: Path | None = None
        self._work: Path | None = None
        self._git: dict[str, Git] = {}

    # --- the repos -----------------------------------------------------------
    def git(self, n: str) -> Git:
        if n not in self._git:
            self._git[n] = Git(self.layout.gitdir(n), self.layout.worktree(n))
        return self._git[n]

    def have(self, n: str) -> bool:
        return self.layout.gitdir(n).is_dir()

    def is_auto(self, n: str) -> bool:
        return self.project.repo(n).auto

    @property
    def work(self) -> Path:
        """Scratch space; plaintext packs pass through here, and close() removes it."""
        if self._work is None:
            self._work = Path(tempfile.mkdtemp(prefix="ferry."))
        return self._work

    def use_keys(self, keys: Keyring) -> None:
        self.keys = keys
        self.hub.keys = keys

    # --- session: lock, scratch, local config --------------------------------
    def begin(self) -> None:
        """One sync at a time on this machine: a post-commit push, a background
        fetch and a session hook can overlap. A session start waits seconds, not
        minutes: a long background download holds the lock, and the session
        should begin from local state rather than stall.

        The lock holds the holder's pid and, where the OS says, when it started:
        a run killed outright leaves its lock behind (a hook timing out, on
        Windows), and the pid may by then be another process's."""
        self.state.mkdir(parents=True, exist_ok=True)
        lock = self.state / "lock"
        tries = {HOOK: 20, BG: 1800}.get(self.ui.mode, 240)    # 10 s, 15 min, 2 min
        n = 0
        while True:
            try:
                lock.mkdir()
                break
            except FileExistsError:
                pid, _, start = _read(lock / "pid").partition("\n")
                if pid.isdigit() and not system.alive(int(pid), start):
                    shutil.rmtree(lock, ignore_errors=True)
                    if not lock.exists():
                        continue
                n += 1
                if n >= tries:
                    self.ui.die(f"another sync is still running (pid {pid or '?'}) — local state left as it is")
                time.sleep(0.5)
        self._lock = lock
        me = os.getpid()
        _write(lock / "pid", f"{me}\n{system.started(me)}")
        self.ensure_local()

    def close(self) -> None:
        if self._lock:
            shutil.rmtree(self._lock, ignore_errors=True)
            self._lock = None
        if self._work:
            shutil.rmtree(self._work, ignore_errors=True)
            self._work = None

    def local_config(self, n: str) -> dict[str, str]:
        """The git config repo n is held to on this machine.

        No line-end conversion anywhere (Git for Windows turns it on by default):
        every file is the same bytes on every machine. The auto repos need that —
        compaction compares trees — and a script checked out with CRLF won't run."""
        if not self.is_auto(n):
            return {"core.hooksPath": str(self.layout.hooks(n)), "core.autocrlf": "false"}
        return {"core.bare": "false", "user.name": "ferry", "user.email": f"ferry@{self.host}",
                "core.autocrlf": "false", "core.safecrlf": "false",
                # PDFs and images don't delta against each other. Past this size git
                # stops trying, which keeps packing new material quick and its memory flat.
                "core.bigFileThreshold": "8m"}

    def ensure_local(self) -> None:
        """Run on every invocation, not just at setup: what a clone can't carry —
        the hooks, excludes, union merges, the auto repos' committer — is
        re-asserted rather than trusted to have survived, so a moved folder heals."""
        for n in self.project.kept:
            if self.have(n):
                self.write_hook(n)
                self.git(n).ensure_config(self.local_config(n))
        for n in self.project.auto:
            g = self.layout.gitdir(n)
            if not g.is_dir():
                continue
            self.layout.worktree(n).mkdir(parents=True, exist_ok=True)
            (g / "info").mkdir(exist_ok=True)
            (g / "info" / "exclude").write_text(".DS_Store\n._*\n.__*\n__pycache__/\n*.py[oc]\n.venv/\n",
                                                encoding="utf-8", newline="\n")
            attributes, union = g / "info" / "attributes", self.project.repo(n).union
            if union:
                attributes.write_text("".join(f"{u} merge=union\n" for u in union), encoding="utf-8", newline="\n")
            else:
                attributes.unlink(missing_ok=True)
            self.git(n).ensure_config(self.local_config(n))

    def write_hook(self, n: str) -> None:
        hook, text = self.layout.hooks(n) / "post-commit", POST_COMMIT.format(repo=n)
        try:
            if hook.read_text(encoding="utf-8") == text:
                return
        except FileNotFoundError:
            hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(text, encoding="utf-8", newline="\n")
        hook.chmod(0o755)

    def use_hub(self) -> None:
        """Make this hub the one this machine syncs through. If it wasn't, forget
        what was known of the last one's slots: which manifests had been read, and
        what this machine's own slot held. Refs of machines not on this hub go at
        the next prune; none of their commits do, so nothing is downloaded again."""
        if _read(self.layout.hub_file) != self.hub.location:
            for d in ("seen", "pushed"):
                shutil.rmtree(self.state / d, ignore_errors=True)
            _write(self.layout.hub_file, self.hub.location)

    def spawn(self, *args: str) -> None:
        system.spawn_detached(["-C", str(self.layout.folder), *args], self.state / "log")

    # --- what this machine holds ---------------------------------------------
    def slotsig(self, n: str) -> str:
        """What this machine's slot should hold: its branches, sealed to the
        current key. The key is in it so that a rekey re-seals repos whose
        branches haven't moved."""
        listing = "".join(f"{sha} {ref}\n" for ref, sha in self.git(n).branches().items())
        return f"{hashlib.sha256(listing.encode()).hexdigest()} {self.keys.recipient}"

    def unpushed(self, n: str) -> bool:
        return self.slotsig(n) != _read(self.state / "pushed" / n)

    def pending(self, n: str) -> bool:
        """Work only this machine holds. A stale slot alone doesn't count: after a
        pull that fast-forwarded, the slot lags but every commit sits in another machine's."""
        g = self.git(n)
        if self.is_auto(n) and g.changes():
            return True
        return self.unpushed(n) and g.any([BRANCHES], [gitcli.glob("refs/hub/*")])

    def ref_hosts(self, n: str, branch: str) -> list[str]:
        """Machines whose copy of branch has been fetched."""
        hosts = []
        for ref in self.git(n).refs("refs/hub/"):
            host, _, b = ref.removeprefix("refs/hub/").partition("/")
            if b == branch:
                hosts.append(host)
        return hosts

    def autocommit(self, n: str) -> None:
        g = self.git(n)
        g.stage_all()
        if g.has_staged():
            g.commit(f"sync: {self.host} {time.strftime('%Y-%m-%d %H:%M')}")
            self.ui.info(f"      {n}: committed local changes")

    # --- pull ----------------------------------------------------------------
    def seen(self, h: str, n: str) -> Path:
        return self.state / "seen" / f"{h}.{n}"

    def fetch(self, n: str, h: str) -> Manifest | None:
        """Bring refs/hub/<h>/* up to that machine's manifest. Returns the manifest
        if it was read."""
        g = self.git(n)
        seen, sig = self.seen(h, n), self.hub.manifest_sig(h, n)
        if sig is None or _read(seen) == sig:
            return None
        m = self.hub.read_manifest(h, n)
        if m is None:
            self.ui.warn(f"{n}: can't read {h}'s copy — that machine may still be on an older key")
            return None
        seen.parent.mkdir(parents=True, exist_ok=True)

        # Which history that copy is on (see compaction, below).
        if self.is_auto(n):
            m.epoch.save(self.seen_epoch(h, n))
            mine = self.epoch(n)
            match plan.peer_epoch(m.epoch.number, mine.number, self.top_epoch(n), m.epoch.epoch == mine.epoch):
                case "behind":
                    self.drop_hub_refs(n, h)
                    if m.epoch.number < mine.number:          # settled once this machine has moved on
                        _write(seen, sig)
                    self.ui.note(f"{n}: {h} is still on the history from before the last compaction — left alone until it moves across")
                    return m
                case "split":
                    self.ui.warn(f"{n}: {h} compacted to epoch {m.epoch.number} separately from this machine — not merging the two; one of them must re-enroll")
                    return m
                case "ahead":
                    self.rebuild_root(n, h, m.epoch.epoch)

        # Only packs bringing commits no local ref reaches. Material another machine
        # already delivered, or that this one wrote, is skipped — and so is the base
        # of a restarted chain, for any machine that kept up.
        wanted = [p for p in m.packs if not g.reaches(p.tips)]
        if not self.budget.take(sum(p.size for p in wanted), f"{n} from {h}"):
            return m
        bundle, got = self.work / "pack.bundle", 0
        for p in wanted:
            if not self.hub.get_pack(h, n, p.name, bundle):
                self.ui.warn(f"{n}: can't read {h}'s pack {p.name} — it was likely replaced mid-pull; the next sync retries")
                return m
            # Every ref a pack carries, into a scratch namespace: a restart's base
            # names its refs refs/sync/base/*, and the manifest decides the real ones
            # below. Until then these keep a half-finished download reachable, so a
            # retry skips the packs it already has.
            try:
                g.fetch_bundle(bundle, f"+refs/*:refs/sync-tmp/{h}/*")
            except GitError as e:
                self.ui.warn(f"{n}: applying {h}'s pack failed — {e.reason}")
                return m
            finally:
                bundle.unlink(missing_ok=True)
            got += p.size

        # The manifest, not the packs, says where that machine's branches are.
        keep = set()
        for sha, ref in m.refs:
            branch = ref.removeprefix("refs/heads/")
            try:
                g.set_ref(f"refs/hub/{h}/{branch}", sha)
            except GitError:
                self.ui.warn(f"{n}: {h}'s {branch} points at a commit that didn't arrive — the next sync retries")
                return m
            keep.add(f"refs/hub/{h}/{branch}")
        g.update_refs({r: None for r in g.refs(f"refs/hub/{h}/", f"refs/sync-tmp/{h}/") if r not in keep})
        _write(seen, sig)
        if wanted:
            self.ui.note(f"{n} ← {h}: {len(wanted)} pack(s), {human(got)}")
        return m

    def peek(self, n: str) -> None:
        """Every changed copy's epoch first, so that one still on an older epoch
        than another is skipped, rather than downloaded and dropped once this one moves."""
        for h in self.hub.others():
            sig = self.hub.manifest_sig(h, n)
            if sig is None or _read(self.seen(h, n)) == sig:
                continue
            m = self.hub.read_manifest(h, n)
            if m is not None:
                self.seen_epoch(h, n).parent.mkdir(parents=True, exist_ok=True)
                m.epoch.save(self.seen_epoch(h, n))

    def fetch_all(self, n: str) -> None:
        self.prune_gone(n)                 # first, so a retired machine's epoch doesn't count
        if self.is_auto(n):
            self.peek(n)
        for h in self.hub.others():
            self.fetch(n, h)

    def prune_gone(self, n: str) -> None:
        """Forget machines whose slot has left the hub."""
        g = self.git(n)
        for ref in g.refs("refs/hub/", "refs/sync-tmp/"):
            h = ref.split("/")[2]
            if not self.hub.has_host(h):
                g.delete_ref(ref)
                for r in self.project.names:
                    self.seen(h, r).unlink(missing_ok=True)
                    self.seen_epoch(h, r).unlink(missing_ok=True)

    def integrate(self, n: str) -> None:
        """Bring the checked-out branch up to every other machine's copy of it.
        Fast-forward is the normal case: one person, one machine at a time. A real
        merge is automatic only in the auto repos: in a kept one it would put a
        commit nobody asked for into a history that is the record."""
        g = self.git(n)
        b = g.branch()
        if b is None:
            self.ui.warn(f"{n}: detached HEAD — not merging")
            return
        for h in self.ref_hosts(n, b):
            ref = f"refs/hub/{h}/{b}"
            if g.is_ancestor(ref, "HEAD"):
                continue
            behind = g.count([ref], ["HEAD"])
            if g.is_ancestor("HEAD", ref):
                if g.fast_forward(ref):
                    self.ui.ok(f"{n} ← {h}: fast-forward, {behind} commit(s)")
                else:
                    self.ui.warn(f"{n}: {h} is {behind} commit(s) ahead, but local edits are in the way — commit or stash them, then sync again")
            elif self.is_auto(n):
                if g.merge(ref, f"sync: merge {h} into {self.host}"):
                    self.ui.ok(f"{n} ← {h}: merged {behind} commit(s)")
                else:
                    self.ui.warn(f"{n}: conflicts with {h}, left unmerged — resolve with ferry git {n} merge {ref}, then run ferry")
            else:
                ahead = g.count(["HEAD"], [ref])
                self.ui.warn(f"{n}: diverged from {h} ({ahead} commit(s) here, {behind} there), not merged automatically — resolve with ferry git {n} merge {ref}, then run ferry")

    def pull(self) -> None:
        for n in self.project.names:
            if not self.have(n):
                continue
            if self.is_auto(n):
                self.autocommit(n)
            self.fetch_all(n)
            if self.adopt(n):
                self.integrate(n)
        _write(self.state / "last-pull", time.strftime("%Y-%m-%d %H:%M"))
        if self.budget.deferred:
            # Detached and fetch-only: it waits for this run's lock, downloads, and
            # moves refs/hub/* — never a working file under an open session. The
            # next session start integrates from local objects in a moment.
            self.spawn("fetch", "--quiet")
            self.ui.ok(f"{human(self.budget.deferred_bytes)} ({', '.join(self.budget.deferred)}) is more than a session start waits for — downloading in the background; it lands at the next session start, or run ferry to wait for it")

    # --- compaction ----------------------------------------------------------
    # The auto repos' history is ferry's own snapshots, which nobody reads, and it
    # costs: a file deleted from one still rides in every chain's base and in
    # every new machine's download. So once old versions outweigh what's worth
    # keeping, a push swaps the history for one parentless commit of the current
    # files, and the repo enters a new epoch (see manifest). Each other machine
    # moves across at its next pull, carrying its own changes over, and then
    # re-pushes; until it has, its slot is left alone rather than merged.
    #
    # No push carries the old history again; each machine drops its own copy
    # FERRY_KEEP_DAYS (14) after the compaction (tidy).
    def epoch_file(self, n: str) -> Path:
        return self.layout.gitdir(n) / "sync-epoch"

    def epoch(self, n: str) -> EpochRecord:
        """The compaction this repo is on here; empty for a kept repo."""
        return EpochRecord.load(self.epoch_file(n)) if self.is_auto(n) else EpochRecord()

    def seen_epoch(self, h: str, n: str) -> Path:
        return self.state / "seen" / f"{h}.{n}.epoch"

    def seen_hosts(self, n: str) -> list[str]:
        """The other machines whose copy of auto repo n was last read here: the
        hub as this machine last saw it."""
        suffix = f".{n}.epoch"
        return sorted(f.name[:-len(suffix)] for f in (self.state / "seen").glob(f"*{suffix}"))

    def top_epoch(self, n: str) -> int:
        """The newest epoch known here, this machine's or another's."""
        return max([self.epoch(n).number] +
                   [EpochRecord.load(f).number for f in (self.state / "seen").glob(f"*.{n}.epoch")])

    def drop_hub_refs(self, n: str, h: str) -> None:
        self.git(n).delete_refs(f"refs/hub/{h}/", f"refs/sync-tmp/{h}/")

    def mkcommit(self, n: str, when: int, message: str, tree: str, parents: list[str]) -> str:
        """The same bytes on every machine that makes it: a fixed name, a given time."""
        return self.git(n).commit_tree(tree, parents, message, author=EPOCH_AUTHOR, date=f"{when} +0000")

    def rebuild_root(self, n: str, h: str, e: Epoch) -> None:
        """A newer epoch's root, made here when this machine holds its files
        already — then the base pack carrying it is skipped like any other reachable one."""
        g = self.git(n)
        if not g.has_commit(e.root):
            if not g.has_tree(e.tree):
                return
            if self.mkcommit(n, e.time, f"sync: compacted, epoch {e.number}", e.tree, []) != e.root:
                return
        g.set_ref(f"refs/sync-tmp/{h}/root", e.root)

    def adopt(self, n: str) -> bool:
        """Move onto the newest epoch another machine compacted to. What this machine
        changed that the compaction didn't see is carried over by a real merge: the
        compacted tree, recast as a commit on top of every cover this machine holds,
        merged into the branch here, has the right merge base for it. False while
        that can't happen yet, so integrate never merges two histories."""
        if not self.is_auto(n):
            return True
        g, mine = self.git(n), self.epoch(n)
        seen = {h: EpochRecord.load(self.seen_epoch(h, n)) for h in self.hub.others()}
        best, _ = plan.newest_epoch(((h, r.number) for h, r in seen.items()), mine.number)
        if best is None:
            return True
        rec = seen[best]
        e = rec.epoch
        if not g.has_commit(e.root):
            self.ui.note(f"{n}: {best} compacted its history; this machine moves across once the download lands")
            return False
        if g.branch() is None:
            self.ui.warn(f"{n}: detached HEAD — not moving onto {best}'s compacted history")
            return False

        carried = ""
        if not g.is_ancestor(e.root, "HEAD"):          # else moved across already; only the record was missing
            covers = [sha for sha in sorted({sha for _, sha in rec.covers}) if g.has_commit(sha)]
            if not covers:
                if g.tree() != e.tree:
                    self.ui.warn(f"{n}: {best} compacted its history, and this machine holds none of what it started from — left as it is; move {self.layout.worktree(n)} aside and run ferry to take {best}'s copy")
                    return False
                tip = e.root
            else:
                before = self.mkcommit(n, e.time, f"sync: as compacted, epoch {e.number}", e.tree, covers)
                g.set_ref("refs/sync/adopt", before)
                if g.is_ancestor("HEAD", before):
                    tip = e.root                                # nothing here the compaction lacks
                else:
                    if not g.is_ancestor(before, "HEAD") and \
                            not g.merge("refs/sync/adopt", f"sync: {self.host}'s changes, ahead of epoch {e.number}"):
                        self.ui.warn(f"{n}: conflicts carrying this machine's changes onto {best}'s compacted history — resolve with ferry git {n} merge refs/sync/adopt, then run ferry")
                        return False
                    if g.tree() == e.tree:
                        tip = e.root
                    else:
                        tip = g.commit_tree(g.tree(), [e.root], f"sync: {self.host}'s changes since epoch {e.number}")
                        carried = " — this machine's own changes kept on top"
            try:
                g.reset_keep(tip)
            except GitError as err:
                self.ui.warn(f"{n}: couldn't move onto {best}'s compacted history — {err.reason}")
                return False
        rec.save(self.epoch_file(n))
        g.delete_ref("refs/sync/adopt")
        for h, r in seen.items():
            if r.number < e.number:
                self.drop_hub_refs(n, h)
        self.ui.ok(f"{n}: moved onto the history {best} compacted (epoch {e.number}){carried}")
        return True

    def tidy(self, n: str) -> None:
        """Some days after a compaction, the history it replaced leaves this machine
        as well. Until then the reflog keeps it as an undo for the move; git's own gc
        would wait for thousands of loose objects, which a quiet repo never reaches."""
        e = self.epoch(n).epoch
        marker = self.layout.gitdir(n) / "sync-tidied"
        done = int(_read(marker) or 0)
        if e is None or e.number <= done or time.time() - e.time < self.settings.keep_days * 86400:
            return
        self.git(n).drop_unreachable()
        _write(marker, str(e.number))
        self.ui.note(f"{n}: the history before epoch {e.number} is gone from this machine too")

    def unlock_compact(self, n: str) -> None:
        """Release the hub-wide lock if this machine holds it and its slot shows the
        epoch it is on."""
        if self.hub.lock_owner(n) != self.host:
            return
        slot = self.hub.read_manifest(self.host, n)
        if (slot.epoch.epoch if slot else None) == self.epoch(n).epoch:
            self.hub.release_lock(n)

    def maybe_compact(self, n: str) -> bool:
        """Compact if it's due, or forced. Only with every other machine on this
        epoch and its latest merged here, read fresh under the lock, since the new
        root must hold everyone's work. True if it compacted."""
        g = self.git(n)
        b = g.branch()
        if not self.is_auto(n) or b is None:
            return False
        mine = self.epoch(n)
        live = g.file_bytes()
        dead = g.history_bytes() - live
        if not self.force:
            if not plan.should_compact(dead=dead, live=live, threshold_mb=self.settings.compact_mb):
                return False
        elif g.count(["HEAD"]) <= 1:
            self.ui.note(f"{n}: already compact")
            return False
        if len(g.branches()) != 1:
            self.ui.note(f"{n}: not compacting — it has more than one branch")
            return False
        owner = self.hub.take_lock(n)
        if owner is not None:
            self.ui.note(f"{n}: not compacting — {owner or 'another machine'} is")
            return False

        covers = []
        for h in self.hub.others():
            if not self.hub.has_manifest(h, n):
                continue
            m = self.hub.read_manifest(h, n)
            sha = m.branch(b) if m else None
            why = None
            if m is None:
                why = f"can't read {h}'s copy"
            elif m.epoch.epoch != mine.epoch:
                why = f"{h} hasn't moved onto the last compaction yet"
            elif sha and not g.is_ancestor(sha, "HEAD"):
                why = f"{h}'s latest isn't merged here yet"
            if why:
                self.ui.note(f"{n}: not compacting — {why}")
                self.unlock_compact(n)
                return False
            if sha:
                covers.append((h, sha))

        tip, tree, when, number = g.resolve("HEAD"), g.tree(), int(time.time()), mine.number + 1
        e = Epoch(number, self.mkcommit(n, when, f"sync: compacted, epoch {number}", tree, []), tree, when)
        g.set_ref(f"refs/heads/{b}", e.root, expect=tip, reason=f"sync: compacted, epoch {e.number}")
        EpochRecord(e, [(self.host, tip), *covers]).save(self.epoch_file(n))
        for h in self.hub.others():
            self.drop_hub_refs(n, h)
        self.compacted = human(dead)
        return True

    # --- push ----------------------------------------------------------------
    def push_one(self, n: str) -> None:
        g = self.git(n)
        mark = self.state / "pushed" / n
        self.compacted = ""
        if self.is_auto(n):
            self.autocommit(n)
            self.unlock_compact(n)
            self.tidy(n)
        if not g.branches():
            return
        sig = self.slotsig(n)
        has_manifest = self.hub.has_manifest(self.host, n)
        if not self.force and has_manifest and _read(mark) == sig:
            return
        self.hub.make_slot(n)
        g.delete_refs("refs/sync/base/")                   # left behind if a push died mid-restart
        if self.maybe_compact(n):
            sig = self.slotsig(n)
        elif has_manifest and _read(mark) == sig:
            return                                         # compact asked for, but it didn't happen

        old = self.hub.read_manifest(self.host, n)
        epoch = self.epoch(n)
        kind, why = plan.choose_push(
            old, manifest_exists=self.hub.has_manifest(self.host, n),
            present=self.hub.packs(self.host, n), recipient=self.keys.recipient,
            maxchain=self.settings.maxchain, epoch=epoch.epoch, compacted=self.compacted,
            history_kept=all(g.has_commit(t) for t in (old.published if old else [])))
        published = old.published if old else []
        bundle = self.work / f"{n}.bundle"
        added: list[Pack] = []

        if kind == plan.FULL:
            g.bundle(bundle, [BRANCHES])
            added.append(self.hub.put_pack(n, bundle, list(g.branches().values())))
        elif kind == plan.RESTART:
            added += self.restart(n, old)
        if kind != plan.FULL and g.count([BRANCHES], published) > 0:
            try:
                g.bundle(bundle, [BRANCHES], published)
            except GitError as e:
                self.ui.die(f"{n}: building a delta failed — {e.reason}")
            added.append(self.hub.put_pack(n, bundle, list(g.branches().values())))

        # The manifest goes last, so it never names a pack that isn't there yet.
        packs = (old.packs if kind == plan.EXTEND else []) + added
        self.hub.put_manifest(n, Manifest(
            recipient=self.keys.recipient, head=g.branch() or "", epoch=epoch,
            refs=[(sha, ref) for ref, sha in g.branches().items()], packs=packs))
        # Only now that the manifest no longer names them: packs a restart
        # replaced, and whatever a push that died midway left behind.
        self.hub.sweep(n, {p.name for p in packs})
        if self.is_auto(n):
            self.unlock_compact(n)
        _write(mark, sig)
        size = human(sum(p.size for p in added))
        if kind == plan.FULL:
            self.ui.ok(f"{n} → hub: {size} full copy ({why})")
        elif kind == plan.RESTART:
            self.ui.ok(f"{n} → hub: chain restarted ({why}) — {size} written, the base being what other machines already hold")
        elif added:
            self.ui.ok(f"{n} → hub: +{size}, pack {len(old.packs) + 1} of the chain")
        else:
            self.ui.ok(f"{n} → hub: branch moves only")

    def restart(self, n: str, old: Manifest) -> list[Pack]:
        """A new chain, built so no machine downloads what it already holds:

          base  per branch, the newest commit every machine is known to reach:
                where this slot last left it, merged down with where each other
                slot stands — read from its manifest now, or from this machine's
                last fetch if that commit isn't here
          step  from there to where this slot last left it

        and push_one adds the usual delta, from there to now. A machine that kept
        up skips base and step; one that fell behind skips the base. Temporary
        refs carry them, since a bundle needs named refs."""
        g, epoch = self.git(n), self.epoch(n).epoch
        bundle, packs = self.work / f"{n}.bundle", []
        peers = {}
        for h in self.hub.others():
            m = self.hub.read_manifest(h, n)
            if m is not None and m.epoch.epoch == epoch:
                peers[h] = m
        base = {}
        for sha, ref in old.refs:
            branch = ref.removeprefix("refs/heads/")
            others = []
            for h in self.hub.others():
                peer = peers[h].branch(branch) if h in peers else None
                if not peer or not g.has_commit(peer):
                    peer = g.resolve(f"refs/hub/{h}/{branch}")
                if peer:
                    others.append(peer)
            base[f"refs/sync/base/{branch}"] = (g.merge_base([sha, *others]) if others else None) or sha
        g.update_refs(base)
        base_tips = list(g.refs("refs/sync/base/").values())
        g.bundle(bundle, [gitcli.glob("refs/sync/base/*")])
        packs.append(self.hub.put_pack(n, bundle, base_tips))
        g.delete_refs("refs/sync/base/")
        if g.count(old.published, base_tips) > 0:
            g.update_refs({f"refs/sync/base/{ref.removeprefix('refs/heads/')}": sha for sha, ref in old.refs})
            g.bundle(bundle, [gitcli.glob("refs/sync/base/*")], base_tips)
            packs.append(self.hub.put_pack(n, bundle, old.published))
            g.delete_refs("refs/sync/base/")
        return packs

    def push(self, only: str | None = None) -> None:
        self.hub.join()
        for n in [only] if only else self.project.names:
            if self.have(n):
                self.push_one(n)

    # --- reports -------------------------------------------------------------
    def dirty_note(self) -> None:
        for n in self.project.kept:
            if self.have(n) and (c := len(self.git(n).changes())):
                self.ui.info(f"      {n}: {c} uncommitted change(s) stay on this machine until committed")

    def last_pull(self) -> str:
        return _read(self.state / "last-pull")

    def hub_down(self) -> None:
        waiting = [n for n in self.project.names if self.have(n) and self.pending(n)]
        not_yet = f" Not on the hub yet: {' '.join(waiting)}." if waiting else ""
        self.ui.warn(f"hub not reachable at {self.hub.location} — working from local state, last pulled "
                     f"{self.last_pull() or 'never'}.{not_yet} Run ferry once it's back.")

    def newest(self, n: str) -> str | None:
        """The machine whose copy of n changed last; for an auto repo, the last
        among those on its latest compaction."""
        best, top = None, -1
        for h in self.hub.by_recency(n):
            if not self.is_auto(n):
                return h
            m = self.hub.read_manifest(h, n)
            if m is not None and m.epoch.number > top:
                best, top = h, m.epoch.number
        return best
