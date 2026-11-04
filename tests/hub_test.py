"""hub_test.py — ferry end to end: fake machines, each with its own HOME and
config folder, syncing through scratch hubs in a temp dir. Nothing outside it
is touched; no real hub or config is ever read. Takes a few minutes.

  uv run python tests/hub_test.py
  FERRY_TEST_SHARE=DIR uv run python tests/hub_test.py

The second puts the scratch hubs in a fresh folder under DIR, a folder on a
real share, to try its locking, renames and listings; the folder goes after
a clean run.

Part 1 is the everyday: enroll, chains, hooks, merges, conflicts, budgets, an
unreachable hub, rekey, retire, a move to a new hub. Part 2 is compaction.
Each prints PASS/FAIL per check; the temp dir is kept when anything fails.

The standard library only: ferry runs as installed, never imported here.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

passed = failed = 0


class Out(str):
    """What a command printed, stdout and stderr together, as $(cmd 2>&1) has
    it, with its exit code."""
    code = 0


def run(cmd, cwd=None, env=None) -> Out:
    p = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = Out(p.stdout.decode("utf-8", "replace").rstrip("\n"))
    out.code = p.returncode
    return out


def check(name, cond) -> None:
    global passed, failed
    try:
        ok, why = bool(cond()), ""
    except Exception as e:                  # a file that isn't there, say: that's a FAIL too
        ok, why = False, f"  ({type(e).__name__}: {e})"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{why}", flush=True)


def on(x, *cmd, cwd=None, **env) -> Out:    # a command as machine x
    return run(cmd, cwd, {**os.environ, "HOME": str(T / x / "home"), "XDG_CONFIG_HOME": str(T / x / "conf"), **env})


def m(x, *args, **env) -> Out:
    return on(x, FERRY, "-C", R(x), *args, **env)


def mg(x, r, *args) -> Out:
    return m(x, "git", r, *args)


def gx(x, *args) -> Out:                    # git as that machine: its commits fire its hooks
    out = on(x, "git", *args)
    if out.code:
        print(f"git {' '.join(map(str, args))}: exit {out.code}\n{out}", flush=True)
    return out


def R(x) -> Path:
    return T / x / "proj"


def key(x) -> Path:                         # where a machine keeps the project's key
    return T / x / "conf" / "ferry" / "proj"


def src(x) -> Path:
    return R(x) / "source"


# Where Claude Code keeps a machine's memory: every character of the folder's
# path but an ASCII letter or digit becomes "-" (no path here passes 200).
def memdir(x) -> Path:
    return T / x / "home" / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(R(x))) / "memory"


def mkdir(*ps) -> None:
    for p in ps:
        Path(p).mkdir(parents=True, exist_ok=True)


def write(p, *lines) -> None:               # printf '%s\n' LINES > p
    Path(p).write_bytes("".join(f"{s}\n" for s in lines).encode())


def append(p, line) -> None:                # echo LINE >> p
    with open(p, "ab") as f:
        f.write(f"{line}\n".encode())


def read(p) -> str:                         # $(cat p)
    return Path(p).read_text(encoding="utf-8").rstrip("\n")


def last(p) -> str:                         # tail -1
    return read(p).split("\n")[-1]


def grep(p, pattern) -> bool:
    return re.search(pattern, read(p), re.M) is not None


def count(out, s) -> int:                   # grep -c
    return sum(s in line for line in out.split("\n"))


def says(out, *parts) -> bool:              # [[ $out == *"a"*"b"* ]]
    i = 0
    for s in parts:
        i = out.find(s, i)
        if i < 0:
            return False
        i += len(s)
    return True


def rnd(p, n) -> None:
    Path(p).write_bytes(os.urandom(n))


def tree(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): p.read_bytes() if p.is_file() else None for p in d.rglob("*")}


def same(a, b) -> bool:                     # diff -r
    a, b = Path(a), Path(b)
    if a.is_dir() or b.is_dir():
        return a.is_dir() and b.is_dir() and tree(a) == tree(b)
    return a.read_bytes() == b.read_bytes()


def rmtree(p) -> None:
    """rm -rf. Git keeps its objects read-only, and Windows won't delete those."""
    def writable(fn, path, _):
        os.chmod(path, stat.S_IWRITE)
        fn(path)
    if os.path.lexists(p):
        shutil.rmtree(p, onexc=writable)


def has(x, sha) -> bool:
    return mg(x, "source", "cat-file", "-e", sha).code == 0


def slot(x, r) -> int:                      # KB in a machine's slot for a repo
    return sum(f.stat().st_size for f in (H / x / r).iterdir()) // 1024


def settle(*xs) -> None:
    time.sleep(2)
    for x in xs:
        while (R(x) / ".ferry" / "lock").is_dir():
            time.sleep(0.5)


# The first line of a slot's manifest: the format's name.
MAGIC = """import sys; from pathlib import Path; from ferry.crypto import Keyring
print(Keyring.load(Path(sys.argv[1])).open_text(Path(sys.argv[2])).split("\\n")[0])"""


def magic(x, r) -> str:                    # print ends its line with \r\n on Windows
    return run([PY, "-c", MAGIC, key("A"), H / x / r / "manifest.age"]).rstrip("\r")


def setup() -> Out:                         # a fresh hub at H, with machine A on it at T
    A = R("A")
    mkdir(T / "A" / "home", T / "A" / "conf", A)
    write(A / "ferry.toml", 'name = "proj"', '[repos.lab]', 'path = "lab"',
          '[repos.memory]', 'claude-memory = true', 'auto = true', 'union = ["MEMORY.md"]',
          '[repos.source]', 'path = "source"', 'auto = true', '[repos.personal]', 'path = "personal"', 'auto = true')
    write(A / ".gitignore", "source/", "personal/", "lab/", ".ferry/"); write(A / "notes.md", "notes")
    gx("A", "init", "-q", A); gx("A", "-C", A, "config", "user.email", "t@t")
    gx("A", "-C", A, "add", "-A"); gx("A", "-C", A, "commit", "-qm", "init")
    gx("A", "init", "-q", A / "lab"); gx("A", "-C", A / "lab", "config", "user.email", "t@t")
    write(A / "lab" / "x.py", "wb"); gx("A", "-C", A / "lab", "add", "-A"); gx("A", "-C", A / "lab", "commit", "-qm", "wb")
    mkdir(A / "source", A / "personal", memdir("A"))
    write(src("A") / "a.txt", "alpha"); rnd(src("A") / "big1.bin", 3000000)
    write(A / "personal" / "g.txt", "grant"); write(memdir("A") / "MEMORY.md", "- one")
    return m("A", "init", "--hub", H, "--host", "A")


def add(x) -> Out:                          # enroll another machine with A's key
    mkdir(T / x / "home", key(x))
    shutil.copyfile(key("A") / "identity", key(x) / "identity")
    out = on(x, FERRY, "enroll", "proj", "--hub", H, "--dest", R(x), "--host", x)
    if out.code == 0:
        gx(x, "-C", R(x), "config", "user.email", "t@t"); gx(x, "-C", R(x) / "lab", "config", "user.email", "t@t")
    return out


# --- part 1: the everyday ------------------------------------------------------
def everyday() -> None:
    out = setup()
    check("init publishes the first machine", lambda: "A is the first machine on the hub" in out and read(H / "FORMAT") == "ferry 1")
    out = add("B"); add("C")
    check("enroll copies every repo", lambda: "B is enrolled: proj is at" in out and same(src("A"), src("B")) and same(memdir("A") / "MEMORY.md", memdir("B") / "MEMORY.md"))

    append(src("A") / "a.txt", "beta"); out = m("A", "sync")
    check("a push extends the chain", lambda: says(out, "source → hub: +", "pack 2 of the chain"))
    out = m("B", "pull", "--hook")
    check("the session hook fast-forwards", lambda: out == "ferry: source ← A: fast-forward, 1 commit(s)")
    out = m("B", "pull", "--hook")
    check("... and is quiet when there's nothing", lambda: out == "ferry: up to date with the hub (other machines: A C)")
    check("manifests name their format", lambda: magic("A", "root") == "ferry manifest 1")

    append(R("A") / "notes.md", "more"); gx("A", "-C", R("A"), "commit", "-qam", "notes"); settle("A")
    check("a root commit pushes from the hook", lambda: "root → hub: +" in read(R("A") / ".ferry" / "log"))
    out = m("B", "sync")
    check("root fast-forwards elsewhere", lambda: "root ← A: fast-forward, 1 commit(s)" in out)
    write(R("B") / "lab" / "y.py", "y"); gx("B", "-C", R("B") / "lab", "add", "-A"); gx("B", "-C", R("B") / "lab", "commit", "-qm", "y"); settle("B")
    out = m("A", "pull")
    check("lab does too", lambda: "lab ← B: fast-forward, 1 commit(s)" in out)

    append(R("B") / "notes.md", "b"); gx("B", "-C", R("B"), "commit", "-qam", "b notes"); settle("B")
    write(R("A") / "other.md", "a"); gx("A", "-C", R("A"), "add", "other.md"); gx("A", "-C", R("A"), "commit", "-qm", "a other"); settle("A")
    out = m("B", "sync")
    check("diverged root is never merged automatically", lambda: "root: diverged from A (1 commit(s) here, 1 there), not merged automatically" in out)
    mg("B", "root", "merge", "-q", "--no-edit", "refs/hub/A/master"); m("B", "sync"); out = m("A", "sync")
    check("... and resolves by hand", lambda: "root ← B: fast-forward, 2 commit(s)" in out)

    write(src("A") / "xa.txt", "fromA"); write(src("B") / "xb.txt", "fromB"); m("A", "sync"); out = m("B", "sync")
    check("auto repos merge", lambda: "source ← A: merged 1 commit(s)" in out)
    m("A", "sync")
    write(src("A") / "a.txt", "A-side"); write(src("B") / "a.txt", "B-side"); m("A", "sync"); out = m("B", "sync")
    check("a conflict is left unmerged", lambda: "source: conflicts with A, left unmerged" in out and read(src("B") / "a.txt") == "B-side")
    mg("B", "source", "merge", "-q", "--no-edit", "refs/hub/A/master"); write(src("B") / "a.txt", "resolved")
    mg("B", "source", "add", "a.txt"); mg("B", "source", "commit", "-qm", "resolved"); m("B", "sync"); m("A", "sync")
    check("... and resolves by hand", lambda: read(src("A") / "a.txt") == "resolved")
    append(memdir("A") / "MEMORY.md", "- from A"); append(memdir("B") / "MEMORY.md", "- from B")
    m("A", "sync"); m("B", "sync"); m("A", "sync")
    check("MEMORY.md merges as a union", lambda: "from A" in read(memdir("A") / "MEMORY.md") and "from B" in read(memdir("A") / "MEMORY.md"))

    m("C", "sync")
    for i in range(1, 5):
        append(src("A") / "a.txt", f"r{i}"); m("A", "push", "--only", "source", FERRY_MAXCHAIN="3")
    check("a full chain restarts", lambda: len(list((H / "A" / "source").glob("*.pack.age"))) <= 3)
    out = m("C", "pull")
    check("a reader that kept up skips the new base", lambda: says(out, "source ← A: ", " KB") and " MB" not in out and last(src("C") / "a.txt") == "r4")

    rnd(src("A") / "big2.bin", 3000000); m("A", "sync")
    out = m("C", "pull", "--hook", FERRY_FOREGROUND_MB="1")
    check("a session start defers a big download", lambda: "is more than a session start waits for — downloading in the background" in out and not (src("C") / "big2.bin").is_file())
    settle("C"); time.sleep(2); settle("C"); m("C", "pull", "--hook")
    check("... which lands at the next one", lambda: (src("C") / "big2.bin").is_file())

    away = H.with_name(f"{H.name}.away")
    os.rename(H, away); append(src("A") / "a.txt", "offline")
    out = m("A", "sync")
    check("an unreachable hub fails at the terminal", lambda: out.code == 1 and says(out, "hub not reachable", "Not on the hub yet: source."))
    out = m("A", "pull", "--hook")
    check("... and only warns the session", lambda: out.code == 0 and out.startswith("ferry: WARNING — hub not reachable"))
    os.rename(away, H); m("A", "sync")

    mkdir(T / "E" / "home", key("E"), T / "E" / "conf" / "ferry" / "other")
    shutil.copy(key("A") / "identity", key("E")); shutil.copy(key("A") / "identity", T / "E" / "conf" / "ferry" / "other")
    def e(*args, cwd=None) -> Out:
        return on("E", FERRY, *args, cwd=cwd)
    out = e("-C", R("A"), "pull", "--hook")
    check("hooks stay silent on a machine that doesn't sync", lambda: out.code == 0 and out == "")
    out = e("pull", "--hook", cwd=T / "E")
    check("... and in a folder that isn't a project", lambda: out.code == 0 and out == "")
    mkdir(T / "E" / "full"); write(T / "E" / "full" / "f", "x")
    out = e("enroll", "proj", "--hub", H, "--host", "E", "--dest", T / "E" / "full")
    check("enroll refuses a folder in use", lambda: "exists and is not empty" in out)
    out = e("enroll", "proj", "--host", "E", "--dest", T / "E" / "new")
    check("enroll needs the hub's folder", lambda: "enroll needs --hub DIR" in out)
    out = e("enroll", "proj", "--hub", H, "--host", "E")
    check("... and the folder the project goes in", lambda: "enroll needs --dest DIR" in out and not R("E").exists())
    out = e("enroll", "proj", "--hub", H, "--host", "B", "--dest", T / "E" / "new")
    check("enroll refuses a name in use", lambda: "already has a machine named B" in out)
    out = e("enroll", "nokey", "--hub", H, "--host", "E", "--dest", T / "E" / "new")
    check("enroll needs the project's key", lambda: says(out, "no key at", "nokey"))
    out = e("enroll", "other", "--hub", H, "--host", "E", "--dest", T / "E" / "other")
    check("enroll checks the hub holds that project", lambda: "holds the project proj, not other" in out)
    F = R("F"); mkdir(F, T / "F" / "home"); gx("F", "init", "-q", F)
    write(F / "ferry.toml", 'name = "loose"', '[repos.data]', 'path = "data"', 'auto = true')
    out = on("F", FERRY, "-C", F, "init", "--hub", T / "F" / "hub")
    check("init refuses a repo folder root doesn't ignore", lambda: "root doesn't ignore .ferry/, data/" in out and not (T / "F" / "conf" / "ferry").is_dir())
    out = on("A", FERRY, "status", cwd=R("A") / "lab")
    check("ferry finds the project from a folder under it", lambda: "A ⇄ " in out and says(out, "lab ", "vs B: same"))
    shutil.copytree(R("A"), T / "A" / "copy", symlinks=True); out = on("A", FERRY, "-C", T / "A" / "copy", "sync"); rmtree(T / "A" / "copy")
    check("a copy of the folder won't publish itself", lambda: out.code == 1 and says(out, "syncs proj at", "a copy must not publish itself as A"))
    out = m("A", "push", "--bogus"); check("unknown flags fail", lambda: "unknown flag: --bogus" in out)
    out = m("A", "compact", "root"); check("root never compacts", lambda: "root keeps its history" in out)
    out = m("A", "retire", "A"); check("a machine can't retire itself", lambda: "retire it from another one" in out)

    out = m("A", "rekey")
    check("rekey re-seals every repo", lambda: count(out, "chain restarted (new key)") == 5)
    write(src("A") / "k.txt", "after-rekey"); m("A", "sync")
    out = m("B", "sync")
    check("the old key can't read the new copies", lambda: "root: can't read A's copy" in out)
    shutil.copyfile(key("A") / "identity", key("B") / "identity")
    shutil.copyfile(key("A") / "identity", key("C") / "identity")
    out = m("B", "sync"); m("C", "sync"); m("A", "sync")
    check("the new key reads them, and re-seals its own", lambda: (src("B") / "k.txt").is_file() and "full copy (its manifest is unreadable — a new key?)" in out)

    m("A", "retire", "C"); m("B", "sync")
    check("retire drops the slot, and others forget it", lambda: not (H / "C").is_dir() and mg("B", "source", "for-each-ref", "refs/hub/C/") == "")
    # A move to another hub: one machine makes it, and each other follows when it
    # sets its path; until then it syncs through the old one.
    H2 = HUBS / "daily-hub2"
    out = m("A", "hub"); check("hub says which hub this machine uses", lambda: Path(out) == H)
    hub = R("B") / ".ferry" / "hub"
    os.rename(hub, f"{hub}.x"); out = m("B", "sync"); os.rename(f"{hub}.x", hub)
    check("... and a machine without one is told how to set it", lambda: says(out, "no hub set", "ferry hub DIR"))
    out = m("A", "init", "--hub", H); check("init makes no hub where there is one", lambda: says(out, "a hub at", "already — ferry hub"))
    write(src("A") / "m.txt", "moved"); out = m("A", "init", "--hub", H2)
    check("init on a machine that syncs is a move", lambda: read(H2 / "FORMAT") == "ferry 1" and count(out, "full copy (first push)") == 5 and Path(m("A", "hub")) == H2)
    write(src("B") / "s.txt", "stayed"); out = m("B", "sync")
    check("... and a machine not moved yet stays apart", lambda: not (src("B") / "m.txt").is_file() and "not reachable" not in out)
    out = m("B", "hub", T / "nowhere"); check("hub needs a hub there", lambda: "no hub at" in out and Path(m("B", "hub")) == H)
    out = m("B", "hub", H2)
    check("it follows with ferry hub, bringing what it did meanwhile", lambda: (src("B") / "m.txt").is_file() and "source ← A: merged" in out
          and all(n.startswith("A.") for n in os.listdir(R("B") / ".ferry" / "seen")))
    m("A", "sync")
    check("every machine ends with the same files", lambda: same(src("A"), src("B")) and same(R("A") / "personal", R("B") / "personal")
          and same(memdir("A") / "MEMORY.md", memdir("B") / "MEMORY.md"))


# --- part 2: compaction --------------------------------------------------------
def compaction() -> None:
    setup(); add("B"); add("C")
    big = mg("A", "source", "rev-parse", "HEAD:big1.bin")
    epoch = ".ferry/source.git/sync-epoch"

    # forced compaction; caught-up B rebuilds the root, behind C carries a local file
    (src("A") / "big1.bin").unlink(missing_ok=True); m("A", "sync"); m("B", "pull")
    write(src("C") / "c.txt", "c-local")
    out = m("A", "compact")
    check("A compacts source, skips memory/personal", lambda: "compacted — 2.9 MB" in out and "memory: already compact" in out)
    check("A slot is small", lambda: slot("A", "source") < 50)
    check("... and starts from a commit every machine can build", lambda: mg("A", "source", "log", "-1", "--format=%an %ae %cn %ce") == "ferry ferry@epoch ferry ferry@epoch")
    check("lock released", lambda: not (H / ".compacting-source").is_dir())
    out = m("B", "sync")
    check("B moves across without downloading source", lambda: "moved onto the history A compacted (epoch 1)" in out and "source ← A" not in out)
    check("no duplicate-parent noise", lambda: "duplicate parent" not in out)
    out = m("C", "sync")
    check("C keeps its local file", lambda: "own changes kept on top" in out and (src("C") / "c.txt").is_file() and not (src("C") / "big1.bin").is_file())
    m("A", "sync")
    check("A gets C's file", lambda: (src("A") / "c.txt").is_file())
    add("D")
    check("D enrolls on epoch 1 without the deleted blob", lambda: not has("D", big) and grep(R("D") / epoch, "^epoch 1 "))

    # conflicting edits across a compaction
    for x in "BCD":
        m(x, "sync")
    m("A", "sync")
    write(src("B") / "a.txt", "alpha-B"); write(src("A") / "a.txt", "alpha-A")
    m("A", "sync"); m("A", "compact", "source")
    append(src("A") / "c.txt", "more")
    out = m("A", "compact", "source")
    check("compaction waits for machines on the old epoch", lambda: "hasn't moved onto the last compaction yet" in out)
    out = m("B", "sync")
    check("B reports the conflict and stays put", lambda: "conflicts carrying" in out and grep(R("B") / epoch, "^epoch 1 "))
    out = m("D", "sync")
    check("D skips B's old-epoch pack", lambda: "B is still on the history" in out and "source ← B" not in out)
    mg("B", "source", "merge", "refs/sync/adopt"); write(src("B") / "a.txt", "alpha-merged")
    mg("B", "source", "add", "a.txt"); mg("B", "source", "commit", "-qm", "resolved")
    out = m("B", "sync")
    check("B moves across after resolving", lambda: "moved onto the history A compacted (epoch 2)" in out)
    m("A", "sync")
    check("A has the resolution", lambda: read(src("A") / "a.txt") == "alpha-merged")

    # threshold-triggered compaction, and a session start over budget
    for x in "BCD":
        m(x, "sync")
    m("A", "sync")
    rnd(src("A") / "keep5.bin", 5000000); rnd(src("A") / "tmp3.bin", 3000000); m("A", "sync")
    tmp3 = mg("A", "source", "rev-parse", "HEAD:tmp3.bin")
    (src("A") / "tmp3.bin").unlink(missing_ok=True)
    out = m("A", "sync")
    check("default threshold: no compaction for 2.9 MB", lambda: "compacted" not in out)
    append(src("A") / "a.txt", "t")
    out = m("A", "sync", FERRY_COMPACT_MB="1")
    check("FERRY_COMPACT_MB=1: compacts on its own", lambda: "compacted — 2.9 MB" in out)
    out = m("B", "pull", "--hook", FERRY_FOREGROUND_MB="1")
    check("B's session start defers the 4.8 MB", lambda: "downloading in the background" in out)
    settle("B"); time.sleep(2); settle("B")
    out = m("B", "pull", "--hook", FERRY_FOREGROUND_MB="1")
    check("next session start moves across", lambda: "moved onto the history A compacted (epoch 3)" in out and (src("B") / "keep5.bin").is_file())
    check("B never got the deleted 3 MB", lambda: not has("B", tmp3))

    # locks, interrupted compaction, retire
    for x in "BCD":
        m(x, "sync")
    m("A", "sync")
    mkdir(H / ".compacting-source"); write(H / ".compacting-source" / "owner", "Z")
    append(src("A") / "a.txt", "x"); m("A", "sync")
    out = m("A", "compact", "source")
    check("someone else's lock holds A off", lambda: "not compacting — Z is" in out)
    rmtree(H / ".compacting-source")
    for x in "BCD":
        m(x, "sync")
    m("A", "sync")
    part = H / "A" / "source" / ".manifest.age.part"
    part.mkdir()                            # so its manifest can't be written
    m("A", "compact", "source"); part.rmdir()
    check("a failed compaction keeps the lock", lambda: read(H / ".compacting-source" / "owner") == "A")
    append(src("B") / "c.txt", "z")
    out = m("B", "compact", "source")
    check("... and holds B off", lambda: "not compacting — A is" in out)
    m("A", "push")
    check("A's next push publishes and unlocks", lambda: not (H / ".compacting-source").is_dir())
    out = m("B", "sync")
    check("B moves across with its later edit", lambda: "own changes kept on top" in out and last(src("B") / "c.txt") == "z")
    mkdir(H / ".compacting-personal"); write(H / ".compacting-personal" / "owner", "C")
    m("A", "retire", "C")
    check("retire drops the retired machine's lock", lambda: not (H / ".compacting-personal").is_dir())

    # tidy, then a chain restart on the new epoch
    m("A", "sync")
    m("A", "push", FERRY_KEEP_DAYS="1")
    check("tidy waits", lambda: has("A", tmp3))
    append(src("A") / "a.txt", "k"); m("A", "push", FERRY_KEEP_DAYS="0")
    check("tidy drops the old history", lambda: not has("A", tmp3) and mg("A", "source", "fsck", "--no-dangling").code == 0)
    for x in "BD":
        m(x, "sync")
    m("A", "sync")
    for i in range(1, 4):
        append(src("B") / "a.txt", f"r{i}"); m("B", "sync", FERRY_MAXCHAIN="2")
    out = m("D", "sync")
    check("restart on epoch 4 is read normally", lambda: last(src("D") / "a.txt") == "r3" and "warn" not in out)
    out = m("D", "status")
    check("status shows the epoch", lambda: says(out, "compacted ", "(epoch 4)"))


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    TOOL = Path(__file__).resolve().parent.parent
    ROOT = Path(tempfile.mkdtemp()).resolve()      # physical, with long names, as ferry resolves it
    HUBS = ROOT
    if share := os.environ.get("FERRY_TEST_SHARE"):
        HUBS = Path(tempfile.mkdtemp(prefix="ferry-test.", dir=share))
        print(f"hubs on the share: {HUBS}")
    for v in ("GIT_DIR", "GIT_WORK_TREE"):
        os.environ.pop(v, None)
    # ferry as installed: this checkout, in a venv of the test's own, on the PATH
    # where the post-commit hooks look for it.
    venv = ROOT / "venv"
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}      # uv run's, not this one
    if subprocess.run(["uv", "sync", "--quiet", "--project", TOOL], env={**env, "UV_PROJECT_ENVIRONMENT": str(venv)}).returncode:
        sys.exit(1)
    BIN = venv / "bin" if (venv / "bin").is_dir() else venv / "Scripts"
    os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ['PATH']}"
    FERRY, PY = shutil.which("ferry", path=BIN), shutil.which("python", path=BIN)

    for part, T, H in ((everyday, ROOT / "daily", HUBS / "daily-hub"), (compaction, ROOT / "compact", HUBS / "compact-hub")):
        try:
            part()
        except Exception:
            failed += 1
            print(f"FAIL  {part.__name__} stopped:\n{traceback.format_exc()}", flush=True)

    print(f"== {passed} passed, {failed} failed")
    if failed:
        print(f"kept for a look: {ROOT} {HUBS}")
    else:
        try:
            rmtree(ROOT); rmtree(HUBS)
        except OSError as e:
            print(f"left behind: {e.filename} ({e.strerror})")
    sys.exit(1 if failed else 0)
