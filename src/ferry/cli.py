"""The commands, and the flags they take."""
from __future__ import annotations

import os
import shutil
import signal
import sys
import time
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path
from typing import NoReturn

from . import gitcli, plan, project, system
from .config import Config, Machine, Settings, valid_name
from .crypto import CryptoError, Keyring, generate_key
from .engine import Sync, unignored
from .gitcli import GitError
from .hub import Hub, HubFormatError
from .manifest import EpochRecord
from .project import ROOT, Layout, Project, ProjectError
from .store import Store, StoreError, store_at
from .ui import BG, HOOK, NORMAL, UI, day, human

HELP = """\
ferry — keep a project's git repos in step across machines through an
encrypted folder on a shared drive. It runs inside the project: the folder
holding ferry.toml, or any folder under it, or the one -C DIR names.

  ferry [sync]                           pull, then push
  ferry pull [--hook] [--background]     --hook: what Claude Code's SessionStart runs
  ferry push [--only REPO] [--background]
  ferry fetch [--background]             download only; no working file changes
  ferry status
  ferry init --hub DIR [--host NAME]     make a hub: the first machine, or the
                                         first to move the project to a new one
  ferry enroll PROJECT --hub DIR --dest DIR [--host NAME]
                                         every other machine; DIR is empty, or
                                         a clone of a root ferry doesn't sync
  ferry hub [DIR]                        the hub's folder as this machine mounts
                                         it, or a new one: a remount, or a move
  ferry compact [REPO...]                compact auto repos now, whatever the size
  ferry git REPO ARGS...                 run git on one repo, e.g. to finish a merge
  ferry retire HOST                      drop a machine's slot from the hub
  ferry rekey                            new key; the old one is kept for reading
  ferry --version

HUB, or DIR after --hub, is the hub's folder as the share is mounted on this
machine, or s3://BUCKET/PREFIX for a hub in S3.

--background returns at once and logs to .ferry/log. The README has the rest:
ferry.toml, the hooks, and how the hub works."""


def same_folder(a: Path, b: Path) -> bool:
    """Whether a and b are one folder however they're spelt: a drive letter's
    case, a junction, a redirected Documents."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


class Run:
    """One invocation: who's listening, the project, this machine's config for it, and the engine."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.ui = UI()
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.only: str | None = None
        self.background = False
        self.machine: Machine | None = None
        self.sync: Sync | None = None

    def die(self, msg: str) -> NoReturn:
        self.ui.die(msg)

    # --- set-up shared by the commands ---------------------------------------
    def flags(self, args: Sequence[str]) -> None:
        it = iter(args)
        for a in it:
            if a == "--hook":
                self.ui.mode = HOOK
            elif a == "--quiet":
                self.ui.mode = BG
            elif a == "--background":
                self.background, self.ui.mode = True, BG
            elif a == "--only":
                self.only = next(it, None) or self.die("--only needs a repo name")
            else:
                self.die(f"unknown flag: {a}")

    def options(self, args: Sequence[str], wanted: dict[str, str], command: str) -> dict[str, str]:
        found, it = {}, iter(args)
        for a in it:
            if a not in wanted:
                self.die(f"unknown {command} flag: {a}")
            found[a] = next(it, None) or self.die(f"{a} needs {wanted[a]}")
        return found

    def hub_option(self, opts: dict[str, str], command: str) -> Store:
        """Where the share is mounted differs by OS, so each machine names its hub."""
        if "--hub" not in opts:
            self.die(f"{command} needs --hub DIR: the hub's folder, as the share is mounted on this machine, "
                     "or s3://BUCKET/PREFIX")
        return store_at(opts["--hub"])

    def check_repo(self, s: Sync, n: str) -> None:
        if n not in s.project.names:
            self.die(f"no repo named '{n}' — one of: {' '.join(s.project.names)}")

    def check_host(self, host: str) -> None:
        if not valid_name(host):
            self.die(f"machine name '{host}' won't do — letters, digits, . _ - only (--host NAME)")

    def need_git(self) -> None:
        v = gitcli.version()
        if v is None:
            self.die("required tool not found: git")
        if v < gitcli.MIN_VERSION:
            self.die(f"git {v[0]}.{v[1]} is too old — ferry needs {'.'.join(map(str, gitcli.MIN_VERSION))} or later")

    def engine(self, layout: Layout, hub: Store, host: str, keys: Keyring) -> Sync:
        if self.sync:
            self.sync.close()
        self.sync = Sync(layout, Hub(hub, host, keys), keys, self.ui, Settings.from_env())
        return self.sync

    def find(self) -> tuple[Path, Project]:
        """The project the current folder is in."""
        folder = project.find(self.cwd)
        if folder is None:
            if self.ui.mode != NORMAL:
                raise SystemExit(0)              # a folder ferry doesn't keep: hooks stay silent
            self.die(f"no {project.FILE} in {self.cwd} or any folder above it — run ferry inside a project, or with -C DIR")
        try:
            proj = Project.load(folder)
        except ProjectError as e:
            self.die(str(e))
        self.machine = Machine.of(proj.name)
        return folder.resolve(), proj

    def load(self, hub: Store | None = None) -> Sync:
        """The engine for the project here, on its hub as this machine has it, or
        on hub."""
        folder, proj = self.find()
        m = self.machine
        cfg = Config.read(m.config)
        if cfg is None:
            if self.ui.mode != NORMAL:
                raise SystemExit(0)              # a machine that doesn't sync this project: hooks stay silent
            self.die(f"this machine doesn't sync {proj.name} yet — ferry init on the first machine, "
                     f"ferry enroll {proj.name} on the others")
        if not same_folder(cfg.repo, folder):
            # Said in hook mode too, not exited silently: a session that never syncs should know.
            self.die(f"this machine syncs {proj.name} at {cfg.repo}, not {folder} — a copy must not publish itself as {cfg.host}")
        if not m.key.is_file():
            self.die(f"no key at {m.key} — copy it from a machine that syncs {proj.name}")
        layout = Layout(folder, proj)
        if hub is None:
            try:
                hub = store_at(layout.hub_file.read_text(encoding="utf-8").strip())
            except FileNotFoundError:
                self.die(f"no hub set for {proj.name} on this machine — ferry hub DIR, DIR being the hub's folder "
                         "as the share is mounted here")
        return self.engine(layout, hub, cfg.host, Keyring.load(m.dir))

    def begin(self, s: Sync) -> None:
        self.need_git()
        s.begin()

    def detach(self, s: Sync, command: str) -> NoReturn:
        """Rerun this command detached, logging to .ferry/log, so a commit or a
        session exit never waits on the share."""
        s.state.mkdir(parents=True, exist_ok=True)
        log = s.state / "log"
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except FileNotFoundError:
            lines = []
        if len(lines) > 400:
            log.write_text("".join(lines[-200:]), encoding="utf-8", newline="\n")
        s.spawn(command, *(["--only", self.only] if self.only else []), "--quiet")
        raise SystemExit(0)

    # --- day to day ----------------------------------------------------------
    def cmd_sync(self, args: Sequence[str]) -> int:
        self.flags(args)
        s = self.load()
        self.begin(s)
        if not s.hub.is_up():
            s.hub_down()
            return 1 if self.ui.mode == NORMAL else 0
        self.ui.hdr(f"pull ← {s.hub.location}")
        self.ui.said = False
        s.pull()
        if not self.ui.said:
            self.ui.ok("nothing new from other machines")
        self.ui.hdr(f"push → {s.hub.location}/{s.host}")
        self.ui.said = False
        s.push()
        if not self.ui.said:
            self.ui.ok("the hub already has everything")
        s.dirty_note()
        return 0

    def cmd_pull(self, args: Sequence[str]) -> int:
        self.flags(args)
        s = self.load()
        if self.background:
            self.detach(s, "pull")
        if self.ui.mode == HOOK:
            s.budget = plan.Budget(s.settings.foreground_mb * plan.MB)
        self.begin(s)
        if not s.hub.is_up():
            s.hub_down()
            return 0
        self.ui.hdr(f"pull ← {s.hub.location}")
        s.pull()
        if not self.ui.said:
            hs = s.hub.others()
            self.ui.ok(f"up to date with the hub ({'other machines: ' + ' '.join(hs) if hs else 'no other machines yet'})")
        return 0

    def cmd_fetch(self, args: Sequence[str]) -> int:
        self.flags(args)
        s = self.load()
        if self.background:
            self.detach(s, "fetch")
        self.begin(s)
        if not s.hub.is_up():
            s.hub_down()
            return 0
        self.ui.hdr(f"fetch ← {s.hub.location}")
        for n in s.project.names:
            if s.have(n):
                s.fetch_all(n)
        if not self.ui.said:
            self.ui.ok("nothing new to download")
        return 0

    def cmd_push(self, args: Sequence[str]) -> int:
        self.flags(args)
        s = self.load()
        if self.only:
            self.check_repo(s, self.only)
        if self.background:
            self.detach(s, "push")
        self.begin(s)
        if not s.hub.is_up():
            s.hub_down()
            return 0
        self.ui.hdr(f"push → {s.hub.location}/{s.host}")
        s.push(self.only)
        if not self.ui.said:
            self.ui.ok("the hub already has everything")
        s.dirty_note()
        return 0

    def cmd_status(self, args: Sequence[str]) -> int:
        s, ui = self.load(), self.ui
        self.begin(s)
        ui.hdr(f"{s.host} ⇄ {s.hub.location}")
        up = s.hub.is_up()
        if up:
            for n in s.project.names:
                if s.have(n):
                    s.fetch_all(n)
        else:
            ui.warn("hub not reachable — comparing against what was last fetched")
        w = max(11, *(len(n) + 2 for n in s.project.names))
        pad = " " * (w + 2)
        if not s.project.syncs_root:
            ui.info(f"{ROOT:<{w}}not synced — it has a remote of its own")
        for n in s.project.names:
            if not s.have(n):
                ui.info(f"{n:<{w}}not set up here")
                continue
            g = s.git(n)
            b = g.branch() or "(detached)"
            line = f"{n:<{w}}{b:<8} {g.short() or '-'}"
            if up and (m := s.hub.read_manifest(s.host, n)):
                line += f"  slot: {len(m.packs)} pack(s), {human(sum(p.size for p in m.packs))}"
            if c := len(g.changes()):
                line += f"  {c} uncommitted"
            if s.pending(n):
                line += f"  {ui.mark('not on the hub yet')}"
            ui.info(line)
            if s.is_auto(n):
                live, rec = g.file_bytes(), s.epoch(n)
                text = f"files {human(live)}, old versions {human(g.history_bytes() - live)}"
                if rec.epoch:
                    text += f", compacted {day(rec.epoch.time)} (epoch {rec.number})"
                ui.info(f"{pad}{text}")
                if up and s.hub.lock_held(n):
                    ui.info(f"{pad}compaction under way on {s.hub.lock_owner(n) or 'an unknown machine'}")
                for h in s.hub.others() if up else s.seen_hosts(n):
                    f = s.seen_epoch(h, n)
                    if f.is_file() and EpochRecord.load(f).number < rec.number:
                        ui.info(f"{pad}vs {h}: {ui.mark('still on the history before the last compaction')} — moves across at its next sync")
            for h in s.ref_hosts(n, b):
                a, z = g.ahead_behind("HEAD", f"refs/hub/{h}/{b}")
                if a == z == 0:
                    state = "same"
                elif a == 0:
                    state = f"{z} behind"
                elif z == 0:
                    state = f"{a} ahead"
                else:
                    state = f"{ui.mark('diverged')}: {a} here, {z} there"
                ui.info(f"{pad}vs {h}: {state}")
        if loose := unignored(s.layout):
            ui.warn(f"root doesn't ignore {', '.join(p + '/' for p in loose)} — a commit there could carry their files")
        ui.info("")
        ui.info(f"last pull: {s.last_pull() or 'never'}")
        return 0

    # --- machines joining and leaving ----------------------------------------
    def cmd_init(self, args: Sequence[str]) -> int:
        """Make a hub, with this machine its first. On a machine that already syncs
        the project that is a move, and each other machine follows with ferry hub."""
        opts = self.options(args, {"--hub": "a directory or s3://BUCKET/PREFIX", "--host": "a name"}, "init")
        hub, ui = self.hub_option(opts, "init"), self.ui
        self.need_git()
        folder, proj = self.find()
        m = self.machine
        cfg = Config.read(m.config)
        if cfg is not None:
            if not same_folder(cfg.repo, folder):
                self.die(f"this machine syncs {proj.name} at {cfg.repo}, not {folder} — a copy must not publish itself as {cfg.host}")
            if opts.get("--host", cfg.host) != cfg.host:
                self.die(f"this machine is {cfg.host} for {proj.name} already")
        host = cfg.host if cfg else opts.get("--host") or system.default_host()
        self.check_host(host)
        if not (folder / ".git").is_dir():
            self.die(f"no git repo at {folder} — the project folder is a repo of its own, root")
        if loose := unignored(Layout(folder, proj)):
            self.die(f"root doesn't ignore {', '.join(p + '/' for p in loose)} — add them to its .gitignore, "
                     "or root would carry their files")
        if Hub(hub, host, None).is_up():
            self.die(f"there's a hub at {hub.location} already — "
                     + (f"ferry hub {hub.location} moves this machine onto it" if cfg
                        else f"ferry enroll {proj.name} --hub {hub.location} joins it"))
        hub.check_vacant()

        ui.hdr("key")
        m.dir.mkdir(parents=True, exist_ok=True)
        m.dir.chmod(0o700)
        if m.key.is_file():
            ui.ok(f"using the key already at {m.key}")
        else:
            generate_key(m.key)
            ui.ok(f"new key at {m.key}")
        s = self.engine(Layout(folder, proj), hub, host, Keyring.load(m.dir))
        s.hub.create()
        Config(host, folder).write(m.config)
        self.begin(s)
        s.use_hub()

        ui.hdr("repos")
        for n in proj.auto:
            if not s.layout.gitdir(n).is_dir():
                gitcli.init(s.layout.gitdir(n), bare=True)
        s.ensure_local()
        for n in proj.auto:
            g = s.git(n)
            s.autocommit(n)
            if g.resolve("HEAD") is None:
                g.commit(f"sync: start on {host}", allow_empty=True)
            ui.ok(f"{n}: {g.tracked_count()} file(s) from {s.layout.worktree(n)}")
        if proj.kept:
            ui.ok(f"{', '.join(proj.kept)}: each commit pushes, through a post-commit hook")
        if not proj.syncs_root:
            ui.ok(f"{ROOT}: not synced — its history travels by a remote of its own")

        ui.hdr(f"push → {hub.location}/{host}")
        s.push()
        if hub.mounted:
            print(f"""
{host} is the first machine on the hub at {hub.location}. A machine that already
syncs {proj.name} moves onto it with ferry hub DIR, DIR being the hub's
folder as the share is mounted there. A new one, with the share mounted and
ferry installed:

  1. copy {m.key} to ~/.config/ferry/{proj.name}/ there
     — AirDrop, USB or scp, never via the share
  2. ferry enroll {proj.name} --hub HUB --dest DIR, HUB being the hub's
     folder as the share is mounted there, and DIR the folder {proj.name}
     goes in""")
        else:
            print(f"""
{host} is the first machine on the hub at {hub.location}. A machine that already
syncs {proj.name} moves onto it with ferry hub {hub.location}. A new one, with
ferry installed and AWS credentials for the bucket:

  1. copy {m.key} to ~/.config/ferry/{proj.name}/ there
     — AirDrop, USB or scp, never via the bucket
  2. ferry enroll {proj.name} --hub {hub.location} --dest DIR, DIR being the
     folder {proj.name} goes in""")
        if not proj.syncs_root:
            print("\nferry doesn't carry root, so clone it there first, and pass the clone as DIR.")
        return 0

    def cmd_enroll(self, args: Sequence[str]) -> int:
        if not args or args[0].startswith("-"):
            self.die("usage: ferry enroll PROJECT --hub DIR --dest DIR [--host NAME]")
        name = args[0]
        if not valid_name(name):
            self.die(f"not a project name: {name}")
        opts = self.options(args[1:], {"--hub": "a directory or s3://BUCKET/PREFIX", "--host": "a name",
                                       "--dest": "a directory"}, "enroll")
        hub, host = self.hub_option(opts, "enroll"), opts.get("--host") or system.default_host()
        if "--dest" not in opts:
            self.die(f"enroll needs --dest DIR: the folder {name} goes in, empty or not there yet")
        dest = Path(opts["--dest"])
        ui, m = self.ui, Machine.of(name)
        self.check_host(host)
        if cloud := system.onedrive(dest):
            self.die(f"{dest} is inside {cloud}, which OneDrive uploads as it is — what the hub only ever holds "
                     "encrypted would leave this machine in the clear; pass --dest DIR outside it")
        self.need_git()
        if m.config.is_file():
            self.die(f"this machine already syncs {name} ({m.config})")
        if not m.key.is_file():
            self.die(f"no key at {m.key} — copy it from a machine that syncs {name} (AirDrop, USB or scp; not via the share)")
        keys = Keyring.load(m.dir)
        if not Hub(hub, host, keys).is_up():
            self.die(f"no hub at {hub.location} — {hub.missing_hint}")
        if Hub(hub, host, keys).has_host(host):
            self.die(f"the hub already has a machine named {host} — pass --host NAME, or retire {host} first if this machine replaces it")
        proj = self.clone_at(dest, name)
        if proj is None and dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
            self.die(f"{dest} exists and is not empty — move it aside first")
        dest.mkdir(parents=True, exist_ok=True)
        folder = dest.resolve()

        ui.hdr(f"copying from {hub.location}")
        if proj is None:
            # Root first: its ferry.toml says what else the project holds.
            self.copy(self.engine(Layout(folder, Project.bare(name)), hub, host, keys), ROOT)
            try:
                proj = Project.load(folder)
            except ProjectError as e:
                self.die(f"{e} — root came from the hub without a ferry.toml ferry can use")
            if proj.name != name:
                self.die(f"the hub at {hub.location} holds the project {proj.name}, not {name}")
        s = self.engine(Layout(folder, proj), hub, host, keys)
        for n in proj.names:
            if n != ROOT:
                self.copy(s, n)
        Config(host, folder).write(m.config)
        self.begin(s)
        s.use_hub()

        ui.hdr("pull")
        s.pull()
        ui.hdr(f"push → {hub.location}/{host}")
        s.push()
        print(f"\n{host} is enrolled: {name} is at {folder}.")
        return 0

    def clone_at(self, dest: Path, name: str) -> Project | None:
        """The project in dest, when dest is a clone of a root ferry doesn't sync:
        enroll then fills in the other repos around it. None for anything else,
        which must be empty, as ever."""
        if not (dest / project.FILE).is_file():
            return None
        try:
            proj = Project.load(dest)
        except ProjectError as e:
            self.die(str(e))
        if proj.syncs_root:
            return None
        if proj.name != name:
            self.die(f"{dest} holds the project {proj.name}, not {name}")
        if not (dest / ".git").is_dir():
            self.die(f"no git repo at {dest} — clone root there, with its .git")
        layout = Layout(dest.resolve(), proj)
        if loose := unignored(layout):
            self.die(f"root doesn't ignore {', '.join(p + '/' for p in loose)} — add them to its .gitignore, "
                     "or root would carry their files")
        # An auto repo's folder found there is moved aside as it is copied, but a
        # kept repo's would be reset onto the hub's copy, losing what's in it.
        for n in proj.kept:
            w = layout.worktree(n)
            if w.exists() and (not w.is_dir() or any(w.iterdir())):
                self.die(f"{w} exists and is not empty — move it aside first; enroll brings {n} from the hub")
        return proj

    def copy(self, s: Sync, n: str) -> None:
        """Repo n, from the machine that wrote it last. The pull that ends enroll
        then takes whatever the others hold beyond that, which is usually nothing."""
        h, ui = s.newest(n), self.ui
        if h is None:
            if n == ROOT:
                self.die("no machine has published the root repo yet — where ferry doesn't sync it "
                         "(sync-root = false), clone it and pass the clone as --dest")
            ui.warn(f"{n}: no machine has published it — skipped")
            return
        w, g = s.layout.worktree(n), s.git(n)
        if s.is_auto(n):
            if w.is_dir() and any(w.iterdir()):
                aside = w.with_name(f"{w.name}.pre-ferry-{self.stamp}")
                w.rename(aside)
                ui.warn(f"moved the existing {w} aside to {aside}")
            w.mkdir(parents=True, exist_ok=True)
            s.state.mkdir(parents=True, exist_ok=True)
            gitcli.init(s.layout.gitdir(n), bare=True)
        else:
            gitcli.init(w)
        g.ensure_config(s.local_config(n))          # before the checkout below, so no line ends are converted
        s.state.mkdir(parents=True, exist_ok=True)
        m = s.fetch(n, h)
        head = (m.head if m else "") or "master"
        sha = g.resolve(f"refs/hub/{h}/{head}") or self.die(f"{n}: {h}'s copy didn't arrive — see the warning above")
        g.set_ref(f"refs/heads/{head}", sha)
        g.check_out(head)
        g.reset_hard()
        seen = s.seen_epoch(h, n)
        if s.is_auto(n) and seen.is_file() and seen.stat().st_size:
            shutil.copyfile(seen, s.epoch_file(n))
        ui.ok(f"{n} ← {h} → {w}")

    def cmd_hub(self, args: Sequence[str]) -> int:
        """The hub's folder as this machine mounts it; or a new one, when the share
        is mounted somewhere else, or the project has moved to a new hub (init on
        another machine). A machine that hasn't moved yet syncs through the old hub."""
        if len(args) > 1 or any(a.startswith("-") for a in args):
            self.die("usage: ferry hub [DIR]")
        if not args:
            print(self.load().hub.location)
            return 0
        hub, ui = store_at(args[0]), self.ui
        s = self.load(hub)
        if not s.hub.is_up():
            self.die(f"no hub at {hub.location} — {hub.missing_hint} (ferry init --hub DIR makes a new one)")
        self.begin(s)
        s.use_hub()
        ui.hdr(f"pull ← {hub.location}")
        ui.said = False
        s.pull()
        if not ui.said:
            ui.ok("nothing new from other machines")
        ui.hdr(f"push → {hub.location}/{s.host}")
        s.push()
        print(f"\n{s.host} syncs {s.project.name} through {hub.location}.")
        return 0

    def cmd_retire(self, args: Sequence[str]) -> int:
        h = args[0] if args else ""
        if not h:
            self.die("usage: ferry retire HOST")
        if not valid_name(h):                   # retire removes <hub>/<h>: no / or \ or .. in it
            self.die(f"not a machine name: {h}")
        s = self.load()
        if h == s.host:
            self.die("that's this machine — retire it from another one")
        self.begin(s)
        if not s.hub.is_up():
            self.die(f"hub not reachable at {s.hub.location}")
        if not s.hub.has_host(h):
            self.die(f"no machine named {h} on the hub (there: {' '.join(s.hub.others())})")
        s.hub.retire(h)
        for n in s.project.names:
            if s.have(n):
                s.prune_gone(n)
        self.ui.ok(f"{h} retired — its slot is gone and its refs forgotten here; other machines forget it on their next pull")
        return 0

    def cmd_rekey(self, args: Sequence[str]) -> int:
        s, m = self.load(), self.machine
        self.begin(s)
        if not s.hub.is_up():
            self.die("hub not reachable — rekey re-encrypts this machine's slot, so it needs the hub")
        old = m.dir / f"identity.old-{self.stamp}"
        m.key.rename(old)
        generate_key(m.key)
        s.use_keys(Keyring.load(m.dir))
        self.ui.hdr(f"re-encrypting {s.hub.location}/{s.host}")
        s.push()
        print(f"""
New key at {m.key}. The old one is kept as {old.name} so this machine
can still read the others until they re-push.

On every other machine: copy the new key over its old one, then run
ferry. Once every machine has synced, delete {m.dir}/identity.old-*.""")
        return 0

    # --- repairs -------------------------------------------------------------
    def cmd_compact(self, args: Sequence[str]) -> int:
        s = self.load()
        for n in args:
            self.check_repo(s, n)
            if not s.is_auto(n):
                self.die(f"{n} keeps its history — only auto repos compact ({' '.join(s.project.auto) or 'none here'})")
        self.begin(s)
        if not s.hub.is_up():
            self.die(f"hub not reachable at {s.hub.location} — compacting needs every machine's latest")
        self.ui.hdr(f"pull ← {s.hub.location}")
        self.ui.said = False
        s.pull()
        if not self.ui.said:
            self.ui.ok("nothing new from other machines")
        self.ui.hdr(f"compact → {s.hub.location}/{s.host}")
        s.force = True
        for n in args or s.project.auto:
            if s.have(n):
                s.push_one(n)
        return 0

    def cmd_git(self, args: Sequence[str]) -> int:
        if not args:
            self.die("usage: ferry git REPO ARGS...")
        n = args[0]
        s = self.load()
        self.check_repo(s, n)
        if not s.have(n):
            self.die(f"{n} isn't set up on this machine")
        return s.git(n).interactive(args[1:])


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The messages hold → ← ⇄ —. On Windows a pipe or a file — the session hook,
    # .ferry/log — would get the ANSI code page, which can't hold them, and CRLF.
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    # A hook that times out is sent SIGTERM; exiting through finally releases the lock.
    # (On Windows it is killed outright instead, and the next run clears the lock.)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    cwd = Path.cwd()
    if argv[:1] == ["-C"]:
        if len(argv) < 2:
            print("-C needs a folder", file=sys.stderr)
            return 1
        cwd, argv = Path(argv[1]).resolve(), argv[2:]
    run = Run(cwd)
    command, args = (argv[0], argv[1:]) if argv else ("sync", [])
    commands = {"sync": run.cmd_sync, "pull": run.cmd_pull, "push": run.cmd_push, "fetch": run.cmd_fetch,
                "status": run.cmd_status, "init": run.cmd_init, "enroll": run.cmd_enroll, "hub": run.cmd_hub,
                "compact": run.cmd_compact, "git": run.cmd_git, "retire": run.cmd_retire, "rekey": run.cmd_rekey}
    if command in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if command in ("-V", "--version", "version"):
        print(f"ferry {metadata.version('ferry')}")
        return 0
    if command not in commands:
        print(HELP)
        return 1
    try:
        return commands[command](args)
    except (GitError, CryptoError, HubFormatError, ProjectError, StoreError) as e:
        run.die(str(e))
    except OSError as e:                    # the share went away mid-run, a full disk, ...
        run.die(f"{e.strerror} — {e.filename}" if e.filename else str(e))
    except KeyboardInterrupt:
        return 130
    finally:
        if run.sync:
            run.sync.close()
