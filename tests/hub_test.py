"""hub_test.py — ferry end to end: fake machines, each with its own HOME and
config folder, syncing through scratch hubs in a temp dir. Nothing outside it
is touched; no real hub or ferry config is ever read, and only for a hub in S3
your AWS sign-in. Takes a few minutes.

  uv run python tests/hub_test.py
  FERRY_TEST_SHARE=DIR uv run python tests/hub_test.py
  FERRY_TEST_S3=s3://BUCKET/PREFIX uv run python tests/hub_test.py

The second puts the scratch hubs in a fresh folder under DIR, a folder on a
real share, to try its locking, renames and listings; the folder goes after
a clean run. The third puts them under a fresh prefix in an S3 bucket that is
there already, signed in as whoever runs the test (aws login, a profile, ...);
the prefix goes after a clean run.

Part 1 is the everyday: enroll, chains, hooks, merges, conflicts, budgets, an
unreachable hub, rekey, retire, a move to a new hub. Part 2 is compaction.
Each prints PASS/FAIL per check; the temp dir is kept when anything fails.

The standard library only: ferry runs as installed, never imported here, and
S3 is reached through the test venv's own boto3, as ferry would reach it.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

passed = failed = skipped = 0


class Out(str):
    """What a command printed, stdout and stderr together, as $(cmd 2>&1) has
    it, with its exit code."""
    code = 0


def run(cmd, cwd=None, env=None) -> Out:
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
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


def skip(name, why) -> None:
    global skipped
    skipped += 1
    print(f"SKIP  {name}  ({why})", flush=True)


def on(x, *cmd, cwd=None, **env) -> Out:    # a command as machine x
    return run(cmd, cwd, {**machines_env(), "HOME": str(T / x / "home"), "XDG_CONFIG_HOME": str(T / x / "conf"), **env})


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


def stored(x, sha) -> int:                  # bytes git took to store an object of source
    out = mg(x, "source", "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objectsize:disk)")
    return int(dict(line.split() for line in out.split("\n"))[sha])


def drops(out, sha) -> bool:
    """Whether a compaction on A says it dropped that blob. Checked against git's
    own figure for it rather than a number, since that turns on the zlib git has:
    zlib-ng's fastest level, the one for loose objects, makes random bytes 5% bigger."""
    said = re.search(r"compacted — ([\d.]+) MB", out)
    return said is not None and abs(float(said[1]) - stored("A", sha) / 2**20) < 0.1


def slot(x, r) -> int:                      # KB in a machine's slot for a repo
    return H.kb(f"{x}/{r}")


def settle(*xs) -> None:
    time.sleep(2)
    for x in xs:
        while (R(x) / ".ferry" / "lock").is_dir():
            time.sleep(0.5)


# The first line of a slot's manifest: the format's name.
MAGIC = """import sys; from pathlib import Path; from ferry.crypto import Keyring
print(Keyring.load(Path(sys.argv[1])).open_text(Path(sys.argv[2])).split("\\n")[0])"""


def magic(x, r) -> str:                    # print ends its line with \r\n on Windows
    with H.local(f"{x}/{r}/manifest.age") as f:
        return run([PY, "-c", MAGIC, key("A"), f]).rstrip("\r")


# --- the hub, in a folder or in S3 ---------------------------------------------
# What the test reads on a hub and plants there, by key: a path under the hub
# with / between its parts, as ferry's store names them. str(hub) is what
# --hub takes and ferry hub says.
class FolderHub:
    def __init__(self, path: Path):
        self.path = path

    def __str__(self) -> str:
        return str(self.path)

    def is_named(self, out) -> bool:        # ferry hub's answer is this hub
        return Path(out) == self.path

    def read(self, key) -> str:
        return read(self.path / key)

    def exists(self, key) -> bool:
        return (self.path / key).exists()

    def files(self, folder, suffix) -> list:
        return [f.name for f in (self.path / folder).glob(f"*{suffix}")]

    def kb(self, folder) -> int:
        return sum(f.stat().st_size for f in (self.path / folder).iterdir()) // 1024

    @contextmanager
    def local(self, key):
        yield self.path / key

    def plant(self, key, text) -> None:
        mkdir((self.path / key).parent); write(self.path / key, text)

    def remove(self, key) -> None:
        rmtree(self.path / key)

    @contextmanager
    def unreachable(self):                  # the environment a machine sees it in
        away = self.path.with_name(f"{self.path.name}.away")
        os.rename(self.path, away)
        try:
            yield {}
        finally:
            os.rename(away, self.path)

    @contextmanager
    def manifest_blocked(self, slot):       # a slot whose manifest can't be written: its .part is a folder
        part = self.path / slot / ".manifest.age.part"
        part.mkdir()
        try:
            yield True
        finally:
            part.rmdir()


# boto3 as the test venv has it, run as whoever runs the test: their own HOME,
# and their own AWS sign-in.
S3 = """import json, sys, boto3
cmd, url = sys.argv[1], sys.argv[2]
session = boto3.Session()
if cmd == "creds":
    c = session.get_credentials().get_frozen_credentials()
    print(json.dumps({"AWS_ACCESS_KEY_ID": c.access_key, "AWS_SECRET_ACCESS_KEY": c.secret_key,
                      "AWS_SESSION_TOKEN": c.token or "", "AWS_REGION": session.region_name or ""}))
    sys.exit()
bucket, _, key = url.removeprefix("s3://").partition("/")
s3 = session.client("s3")
under = [o for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key + "/")
         for o in page.get("Contents", [])] if cmd in ("ls", "rm") else []
if cmd == "ls":
    for o in under:
        print(o["Size"], o["Key"][len(key) + 1:])
elif cmd == "get":
    s3.download_file(bucket, key, sys.argv[3])
elif cmd == "put":
    s3.put_object(Bucket=bucket, Key=key, Body=sys.argv[3].encode())
elif cmd == "rm":
    for i in range(0, len(under), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": o["Key"]} for o in under[i:i + 1000]]})
"""


def s3(*args) -> Out:
    out = run([PY, "-c", S3, *args])
    if out.code:
        raise RuntimeError(f"s3 {args[0]} {args[1]}: {out}")
    return out


class S3Hub:
    def __init__(self, url: str):
        self.url = url

    def __str__(self) -> str:
        return self.url

    def is_named(self, out) -> bool:
        return out == self.url

    def _ls(self, folder) -> dict:          # every object under folder: its name there, and size
        return {name: int(size) for size, _, name in (line.partition(" ") for line in s3("ls", f"{self.url}/{folder}").split("\n") if line)}

    def read(self, key) -> str:
        with self.local(key) as f:
            return read(f)

    def exists(self, key) -> bool:
        return bool(self._ls(key))

    def files(self, folder, suffix) -> list:
        return [n for n in self._ls(folder) if "/" not in n and n.endswith(suffix)]

    def kb(self, folder) -> int:
        return sum(size for n, size in self._ls(folder).items() if "/" not in n) // 1024

    @contextmanager
    def local(self, key):
        d = Path(tempfile.mkdtemp(prefix="ferry-test-s3."))
        try:
            s3("get", f"{self.url}/{key}", d / "file")
            yield d / "file"
        finally:
            rmtree(d)

    def plant(self, key, text) -> None:
        s3("put", f"{self.url}/{key}", f"{text}\n")

    def remove(self, key) -> None:
        s3("rm", f"{self.url}/{key}")

    @contextmanager
    def unreachable(self):                  # S3 at an address where nothing answers
        yield {"AWS_ENDPOINT_URL": "http://127.0.0.1:9"}

    @contextmanager
    def manifest_blocked(self, slot):       # S3 offers no way to make one write fail
        yield False


def hub_at(name):
    return S3Hub(f"{HUBS}/{name}") if isinstance(HUBS, str) else FolderHub(HUBS / name)


_aws, _aws_at = {}, 0.0


def machines_env() -> dict:
    """The environment every machine starts from. With a hub in S3, the AWS
    credentials of whoever runs the test, fetched afresh every few minutes:
    no machine's HOME holds an ~/.aws, and a sign-in such as aws login hands
    out credentials that last minutes."""
    global _aws, _aws_at
    if not isinstance(HUBS, str):
        return dict(os.environ)
    if time.time() - _aws_at > 180:
        _aws, _aws_at = {k: v for k, v in json.loads(s3("creds", "")).items() if v}, time.time()
    keep = {k: v for k, v in os.environ.items() if not k.startswith("AWS_") or k.startswith(("AWS_ENDPOINT_URL", "AWS_CA_BUNDLE"))}
    return {**keep, **_aws}


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
    check("init publishes the first machine", lambda: "A is the first machine on the hub" in out and H.read("FORMAT") == "ferry 1")
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
    check("a full chain restarts", lambda: len(H.files("A/source", ".pack.age")) <= 3)
    out = m("C", "pull")
    check("a reader that kept up skips the new base", lambda: says(out, "source ← A: ", " KB") and " MB" not in out and last(src("C") / "a.txt") == "r4")

    rnd(src("A") / "big2.bin", 3000000); m("A", "sync")
    out = m("C", "pull", "--hook", FERRY_FOREGROUND_MB="1")
    check("a session start defers a big download", lambda: "is more than a session start waits for — downloading in the background" in out and not (src("C") / "big2.bin").is_file())
    settle("C"); time.sleep(2); settle("C"); m("C", "pull", "--hook")
    check("... which lands at the next one", lambda: (src("C") / "big2.bin").is_file())

    with H.unreachable() as away:
        append(src("A") / "a.txt", "offline")
        out = m("A", "sync", **away)
        check("an unreachable hub fails at the terminal", lambda: out.code == 1 and says(out, "hub not reachable", "Not on the hub yet: source."))
        out = m("A", "pull", "--hook", **away)
        check("... and only warns the session", lambda: out.code == 0 and out.startswith("ferry: WARNING — hub not reachable"))
        out = m("A", "status", **away)
        check("... and status reports from what was last fetched", lambda: out.code == 0
              and says(out, "comparing against what was last fetched", "source ", "vs B: ", "last pull:"))
    m("A", "sync")

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
    check("retire drops the slot, and others forget it", lambda: not H.exists("C") and mg("B", "source", "for-each-ref", "refs/hub/C/") == "")
    # A move to another hub: one machine makes it, and each other follows when it
    # sets its path; until then it syncs through the old one.
    H2 = hub_at("daily-hub2")
    out = m("A", "hub"); check("hub says which hub this machine uses", lambda: H.is_named(out))
    hub = R("B") / ".ferry" / "hub"
    os.rename(hub, f"{hub}.x"); out = m("B", "sync"); os.rename(f"{hub}.x", hub)
    check("... and a machine without one is told how to set it", lambda: says(out, "no hub set", "ferry hub DIR"))
    out = m("A", "init", "--hub", H); check("init makes no hub where there is one", lambda: says(out, "a hub at", "already — ferry hub"))
    write(src("A") / "m.txt", "moved"); out = m("A", "init", "--hub", H2)
    check("init on a machine that syncs is a move", lambda: H2.read("FORMAT") == "ferry 1" and count(out, "full copy (first push)") == 5 and H2.is_named(m("A", "hub")))
    write(src("B") / "s.txt", "stayed"); out = m("B", "sync")
    check("... and a machine not moved yet stays apart", lambda: not (src("B") / "m.txt").is_file() and "not reachable" not in out)
    out = m("B", "hub", T / "nowhere"); check("hub needs a hub there", lambda: "no hub at" in out and H.is_named(m("B", "hub")))
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
    check("A compacts source, skips memory/personal", lambda: drops(out, big) and "memory: already compact" in out)
    check("A slot is small", lambda: slot("A", "source") < 50)
    check("... and starts from a commit every machine can build", lambda: mg("A", "source", "log", "-1", "--format=%an %ae %cn %ce") == "ferry ferry@epoch ferry ferry@epoch")
    check("lock released", lambda: not H.exists(".compacting-source"))
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
    check("default threshold: no compaction for 3 MB", lambda: "compacted" not in out)
    append(src("A") / "a.txt", "t")
    out = m("A", "sync", FERRY_COMPACT_MB="1")
    check("FERRY_COMPACT_MB=1: compacts on its own", lambda: drops(out, tmp3))
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
    H.plant(".compacting-source/owner", "Z")
    append(src("A") / "a.txt", "x"); m("A", "sync")
    out = m("A", "compact", "source")
    check("someone else's lock holds A off", lambda: "not compacting — Z is" in out)
    H.remove(".compacting-source")
    for x in "BCD":
        m(x, "sync")
    m("A", "sync")
    with H.manifest_blocked("A/source") as blocked:
        m("A", "compact", "source")
    append(src("B") / "c.txt", "z")
    if blocked:
        check("a failed compaction keeps the lock", lambda: H.read(".compacting-source/owner") == "A")
        out = m("B", "compact", "source")
        check("... and holds B off", lambda: "not compacting — A is" in out)
        m("A", "push")
        check("A's next push publishes and unlocks", lambda: not H.exists(".compacting-source"))
    else:                                   # A compacted, then; B's edit is still on the old history
        for name in ("a failed compaction keeps the lock", "... and holds B off", "A's next push publishes and unlocks"):
            skip(name, "a hub in folders only: S3 offers no way to make one write fail")
    out = m("B", "sync")
    check("B moves across with its later edit", lambda: "own changes kept on top" in out and last(src("B") / "c.txt") == "z")
    H.plant(".compacting-personal/owner", "C")
    m("A", "retire", "C")
    check("retire drops the retired machine's lock", lambda: not H.exists(".compacting-personal"))

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
    share, bucket = os.environ.get("FERRY_TEST_SHARE"), os.environ.get("FERRY_TEST_S3")
    if share and bucket:
        sys.exit("FERRY_TEST_SHARE or FERRY_TEST_S3, not both")
    if share:
        HUBS = Path(tempfile.mkdtemp(prefix="ferry-test.", dir=share))
        print(f"hubs on the share: {HUBS}")
    for v in ("GIT_DIR", "GIT_WORK_TREE"):
        os.environ.pop(v, None)
    # ferry as installed: this checkout, in a venv of the test's own, on the PATH
    # where the post-commit hooks look for it.
    venv = ROOT / "venv"
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}      # uv run's, not this one
    extra = ["--extra", "s3"] if bucket else []
    if subprocess.run(["uv", "sync", "--quiet", "--project", TOOL, *extra], env={**env, "UV_PROJECT_ENVIRONMENT": str(venv)}).returncode:
        sys.exit(1)
    BIN = venv / "bin" if (venv / "bin").is_dir() else venv / "Scripts"
    os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ['PATH']}"
    FERRY, PY = shutil.which("ferry", path=BIN), shutil.which("python", path=BIN)
    if bucket:
        if not bucket.startswith("s3://"):
            sys.exit(f"FERRY_TEST_S3 is s3://BUCKET/PREFIX, not {bucket}")
        HUBS = f"{bucket.rstrip('/')}/ferry-test.{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        try:
            machines_env()
        except RuntimeError as e:
            sys.exit(f"no AWS credentials to test with — {e}")
        print(f"hubs in S3: {HUBS}")

    for part, T, H in ((everyday, ROOT / "daily", hub_at("daily-hub")), (compaction, ROOT / "compact", hub_at("compact-hub"))):
        try:
            part()
        except Exception:
            failed += 1
            print(f"FAIL  {part.__name__} stopped:\n{traceback.format_exc()}", flush=True)

    print(f"== {passed} passed, {failed} failed" + (f", {skipped} skipped" if skipped else ""))
    if failed:
        print(f"kept for a look: {ROOT} {HUBS}")
    else:
        try:
            if isinstance(HUBS, str):
                s3("rm", HUBS)                  # first: the venv it runs in is under ROOT
            else:
                rmtree(HUBS)
            rmtree(ROOT)
        except OSError as e:
            print(f"left behind: {e.filename} ({e.strerror})")
        except RuntimeError as e:
            print(f"left behind in S3: {HUBS} ({e})")
    sys.exit(1 if failed else 0)
