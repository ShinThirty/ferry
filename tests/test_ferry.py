"""Unit tests for ferry: ferry.toml, the manifest format, ferry's decisions,
the key file, and the layering rules. uv run python -m unittest discover tests
runs them.

These cover what can be checked without a hub. ferry's behaviour end to end —
machines, a share, compaction — is tests/hub_test.py.
"""
import ast
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from ferry import plan, system
from ferry.crypto import Keyring, generate_key
from ferry.hub import Hub, HubFormatError
from ferry.manifest import Epoch, EpochRecord, Manifest, Pack
from ferry.project import Layout, Project, ProjectError, claude_slug, find
from ferry.store import FolderStore, Store, StoreError, store_at

try:                                        # the s3 extra, and moto from the dev group
    import boto3
    from botocore.config import Config
    from moto import mock_aws

    from ferry.s3 import S3Store
except ModuleNotFoundError:
    S3Store = None
from ferry.ui import human

LIB = Path(__file__).resolve().parent.parent / "src" / "ferry"
A, B, C = "a" * 40, "b" * 40, "c" * 40

# A manifest as every machine reads it: its text is the format.
MANIFEST = f"""ferry manifest 1
recipient age1xyz
head master
epoch 2 {A} {B} 1762000000
cover alpha {C}
cover beta {A}
ref {C} refs/heads/master
pack 20261103-101500-123-1.pack.age 5120 {C}
pack 20261103-111500-456-1.pack.age 812 {C} {A}
"""


EXAMPLE = """
name = "notes"

[repos.lab]
path = "lab"

[repos.memory]
claude-memory = true
auto = true
union = ["MEMORY.md"]

[repos.files]
path = "shared/files"
auto = true
"""


class ProjectFile(unittest.TestCase):
    def parse(self, text: str) -> Project:
        return Project.parse(tomllib.loads(text))

    def test_example(self):
        p = self.parse(EXAMPLE)
        self.assertEqual((p.name, p.names), ("notes", ["root", "lab", "memory", "files"]))
        self.assertEqual((p.kept, p.auto), (["root", "lab"], ["memory", "files"]))
        self.assertEqual(p.repo("memory").union, ("MEMORY.md",))

    def test_where_each_repo_lives(self):
        os.environ["HOME"], home = "/h", os.environ.get("HOME")
        try:
            lay = Layout(Path("/p/notes"), self.parse(EXAMPLE))
            self.assertEqual(lay.worktree("root"), Path("/p/notes"))
            self.assertEqual(lay.gitdir("lab"), Path("/p/notes/lab/.git"))
            self.assertEqual(lay.worktree("files"), Path("/p/notes/shared/files"))
            self.assertEqual(lay.gitdir("files"), Path("/p/notes/.ferry/files.git"))
            self.assertEqual(lay.worktree("memory"), Path("/h/.claude/projects/-p-notes/memory"))
            self.assertEqual(lay.hooks("lab"), Path("/p/notes/.ferry/hooks/lab"))
            self.assertEqual(lay.hub_file, Path("/p/notes/.ferry/hub"))
        finally:
            if home is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = home

    def test_root_alone(self):
        self.assertEqual(self.parse('name = "x"').names, ["root"])

    def test_mistakes_are_named(self):
        cases = {
            '': "name",
            'name = ".x"': "name",
            'name = "x"\nhub = "/h"': "unknown key 'hub'",
            'name = "x"\ncolour = 1': "unknown key 'colour'",
            'name = "x"\n[repos.root]\npath = "r"': "needs no [repos.root]",
            'name = "x"\n[repos."a b"]\npath = "r"': "letters, digits",
            'name = "x"\n[repos.a]\npath = "a"\nauto = "yes"': "true or false",
            'name = "x"\n[repos.a]\npath = "a"\ncolour = 1': "unknown key 'colour'",
            'name = "x"\n[repos.a]\nauto = true': "one or the other",
            'name = "x"\n[repos.a]\npath = "a"\nclaude-memory = true': "one or the other",
            'name = "x"\n[repos.a]\npath = "a"\nunion = ["M.md"]': "union is for auto repos",
            'name = "x"\n[repos.a]\npath = "../a"': "a folder inside the project",
            'name = "x"\n[repos.a]\npath = "/a"': "a folder inside the project",
            'name = "x"\n[repos.a]\npath = "a\\\\b"': "a folder inside the project",
            'name = "x"\n[repos.a]\npath = ".ferry/a"': "a folder inside the project",
            'name = "x"\n[repos.a]\npath = "a"\n[repos.b]\npath = "a/b"': "overlap",
            'name = "x"\n[repos.a]\npath = "a"\n[repos.b]\npath = "a"': "overlap",
            'name = "x"\n[repos.a]\nclaude-memory = true\n[repos.b]\nclaude-memory = true': "only one",
        }
        for text, said in cases.items():
            with self.assertRaises(ProjectError, msg=text) as e:
                self.parse(text)
            self.assertIn(said, str(e.exception), text)

    def test_found_from_any_folder_under_it(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "lab" / "deep").mkdir(parents=True)
            self.assertIsNone(find(d / "lab" / "deep"))
            (d / "ferry.toml").write_text(EXAMPLE, encoding="utf-8")
            self.assertEqual(find(d / "lab" / "deep"), d)
            self.assertEqual(Project.load(d).name, "notes")
            (d / "ferry.toml").write_text("name = ", encoding="utf-8")
            with self.assertRaises(ProjectError):
                Project.load(d)


class ManifestFormat(unittest.TestCase):
    def test_round_trip_is_byte_for_byte(self):
        self.assertEqual(Manifest.parse(MANIFEST).text(), MANIFEST)

    def test_fields(self):
        m = Manifest.parse(MANIFEST)
        self.assertEqual(m.epoch.epoch, Epoch(2, A, B, 1762000000))
        self.assertEqual(m.epoch.covers, [("alpha", C), ("beta", A)])
        self.assertEqual(m.branch("master"), C)
        self.assertIsNone(m.branch("main"))
        self.assertEqual(m.published, [C])
        self.assertEqual(m.packs[1], Pack("20261103-111500-456-1.pack.age", 812, [C, A]))

    def test_detached_head_and_no_epoch(self):
        text = f"ferry manifest 1\nrecipient age1xyz\nhead \nref {A} refs/heads/x\n"
        m = Manifest.parse(text)
        self.assertEqual((m.head, m.epoch.number, m.epoch.epoch), ("", 0, None))
        self.assertEqual(m.text(), text)

    def test_rejects_other_formats(self):
        with self.assertRaises(ValueError):
            Manifest.parse("ferry manifest 0\n")
        with self.assertRaises(ValueError):
            Manifest.parse("ferry manifest 2\n")

    def test_epoch_record_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sync-epoch"
            self.assertEqual(EpochRecord.load(f).number, 0)
            rec = Manifest.parse(MANIFEST).epoch
            rec.save(f)
            self.assertEqual(f.read_bytes(), f"epoch 2 {A} {B} 1762000000\ncover alpha {C}\ncover beta {A}\n".encode())
            self.assertEqual(EpochRecord.load(f), rec)


class ChoosePush(unittest.TestCase):
    def choose(self, old, **kw):
        args = dict(manifest_exists=old is not None, present={p.name for p in old.packs} if old else set(),
                    recipient="age1xyz", maxchain=24, history_kept=True,
                    epoch=old.epoch.epoch if old else None, compacted="")
        return plan.choose_push(old, **(args | kw))

    def setUp(self):
        self.old = Manifest.parse(MANIFEST)

    def test_first_push_and_unreadable(self):
        self.assertEqual(self.choose(None), (plan.FULL, "first push"))
        self.assertEqual(self.choose(None, manifest_exists=True), (plan.FULL, "its manifest is unreadable — a new key?"))

    def test_extend(self):
        self.assertEqual(self.choose(self.old)[0], plan.EXTEND)

    def test_restarts(self):
        self.assertEqual(self.choose(self.old, present=set()), (plan.RESTART, "a pack went missing"))
        self.assertEqual(self.choose(self.old, recipient="age1new"), (plan.RESTART, "new key"))
        self.assertEqual(self.choose(self.old, maxchain=2), (plan.RESTART, "the chain reached 2 packs"))

    def test_full_outranks_restart(self):
        self.assertEqual(self.choose(self.old, maxchain=2, history_kept=False), (plan.FULL, "history was rewritten"))
        empty = Manifest.parse("ferry manifest 1\nrecipient age1xyz\nhead master\n")
        self.assertEqual(self.choose(empty, epoch=None), (plan.FULL, "its manifest was empty"))

    def test_epoch_change(self):
        newer = Epoch(3, C, B, 1762100000)
        self.assertEqual(self.choose(self.old, epoch=newer, compacted="2.9 MB"),
                         (plan.FULL, "compacted — 2.9 MB of old versions dropped"))
        self.assertEqual(self.choose(self.old, epoch=newer), (plan.FULL, "moved onto compacted history"))


class Decisions(unittest.TestCase):
    def test_should_compact(self):
        mb = plan.MB
        self.assertFalse(plan.should_compact(dead=63 * mb, live=0, threshold_mb=64))
        self.assertTrue(plan.should_compact(dead=64 * mb, live=256 * mb, threshold_mb=64))
        self.assertFalse(plan.should_compact(dead=64 * mb, live=257 * mb, threshold_mb=64))

    def test_peer_epoch(self):
        self.assertEqual(plan.peer_epoch(1, 2, 2, False), "behind")
        self.assertEqual(plan.peer_epoch(2, 2, 2, False), "split")
        self.assertEqual(plan.peer_epoch(3, 2, 3, False), "ahead")
        self.assertEqual(plan.peer_epoch(2, 2, 2, True), "same")

    def test_newest_epoch_takes_the_first_of_a_tie(self):
        self.assertEqual(plan.newest_epoch([("B", 2), ("C", 3), ("D", 3)], 1), ("C", 3))
        self.assertEqual(plan.newest_epoch([("B", 1)], 1), (None, 1))

    def test_budget(self):
        b = plan.Budget(10)
        self.assertTrue(b.take(6, "source from B"))
        self.assertFalse(b.take(6, "memory from B"))
        self.assertTrue(b.take(4, "personal from B"))
        self.assertEqual((b.remain, b.deferred, b.deferred_bytes), (0, ["memory from B"], 6))
        self.assertTrue(plan.Budget().take(10**12, "anything"))

    def test_human(self):
        self.assertEqual([human(x) for x in (0, 1023, 1024, 3000000, 5 * 2**30)],
                         ["0 B", "1023 B", "1.0 KB", "2.9 MB", "5.0 GB"])


class Keys(unittest.TestCase):
    def test_key_file_seal_and_open(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            generate_key(d / "identity")
            if os.name != "nt":                 # NTFS has no mode bits; the profile's ACL guards it
                self.assertEqual((d / "identity").stat().st_mode & 0o777, 0o600)
            text = (d / "identity").read_bytes().decode()
            self.assertNotIn("\r", text)
            keys = Keyring.load(d)
            self.assertIn(f"# public key: {keys.recipient}\n", text)
            keys.seal_text("hello", d / "m.age")
            self.assertEqual(keys.open_text(d / "m.age"), "hello")
            # After a rekey the old key still opens what it sealed.
            (d / "identity").rename(d / "identity.old-1")
            generate_key(d / "identity")
            self.assertEqual(Keyring.load(d).open_text(d / "m.age"), "hello")


class StoreContract:
    """What the hub asks of every store. Each kind of store runs these, given
    an empty hub in self.store."""
    store: Store

    def put(self, key: str, data: bytes) -> None:
        with self.store.writing(key) as f:
            f.write_bytes(data)

    def get(self, key: str) -> bytes:
        with self.store.reading(key) as f:
            return f.read_bytes()

    def test_a_file_appears_whole_or_not_at_all(self):
        self.store.mkdir("a/s")
        with self.store.writing("a/s/m") as f:
            f.write_bytes(b"one")
            self.assertIsNone(self.store.stat("a/s/m"))
            self.assertEqual(self.store.files("a/s"), set())
        self.assertEqual(self.get("a/s/m"), b"one")
        with self.assertRaises(RuntimeError), self.store.writing("a/s/m") as f:
            f.write_bytes(b"two, cut short")
            raise RuntimeError
        self.assertEqual(self.get("a/s/m"), b"one")
        self.assertEqual(self.store.files("a/s"), {"m"})
        self.store.clean("a/s")
        self.assertEqual(self.store.files("a/s"), {"m"})

    def test_what_isnt_there(self):
        with self.assertRaises(FileNotFoundError):
            self.get("a/s/m")
        self.assertIsNone(self.store.stat("a/s/m"))
        self.assertEqual((self.store.files("a"), self.store.folders("a")), (set(), set()))
        self.assertFalse(self.store.is_folder("a"))
        self.store.delete("a/s/m")
        self.store.delete_folder("a")

    def test_listings(self):
        self.store.mkdir("a/s")
        self.store.mkdir("b")
        self.put("FORMAT", b"f")
        self.put("a/x", b"x")
        self.assertEqual(self.store.folders(""), {"a", "b"})
        self.assertEqual(self.store.files(""), {"FORMAT"})
        self.assertEqual((self.store.folders("a"), self.store.files("a")), ({"s"}, {"x"}))
        self.assertTrue(self.store.is_folder("b"), "a folder holding nothing yet")
        self.assertFalse(self.store.is_folder("a/x"))
        self.assertIsNotNone(self.store.stat("a/x"))

    def test_delete(self):
        self.store.mkdir("a/s")
        self.put("a/s/m", b"m")
        self.put("a/x", b"x")
        self.store.delete("a/x")
        self.assertEqual(self.store.files("a"), set())
        self.store.delete_folder("a")
        self.assertFalse(self.store.is_folder("a"))
        self.assertEqual(self.store.folders(""), set())

    def test_one_claim_wins(self):
        self.assertTrue(self.store.claim(".lock/owner", b"alpha\n"))
        self.assertFalse(self.store.claim(".lock/owner", b"beta\n"))
        self.assertEqual(self.get(".lock/owner"), b"alpha\n")
        self.store.delete_folder(".lock")
        self.assertTrue(self.store.claim(".lock/owner", b"beta\n"))


class FolderStoreContract(StoreContract, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FolderStore(Path(self.tmp.name) / "hub")
        self.store.mkdir("")

    def tearDown(self):
        self.tmp.cleanup()


class MockS3:
    """A bucket in moto's S3, in this process: nothing leaves the machine, and
    no AWS setting of the user's is read."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                                           "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1"})
        env.start()
        self.addCleanup(env.stop)
        for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
            os.environ.pop(k, None)
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        self.client = boto3.client("s3")
        self.client.create_bucket(Bucket="ferry-test")
        self.store = S3Store("ferry-test", "hub/")
        self.store.mkdir("")


@unittest.skipIf(S3Store is None, "needs moto, from the dev group: uv sync")
class S3StoreContract(MockS3, StoreContract, unittest.TestCase):
    pass


@unittest.skipIf(S3Store is None, "needs moto, from the dev group: uv sync")
class S3Files(MockS3, unittest.TestCase):
    def put(self, key: str, data: bytes) -> None:
        with self.store.writing(key) as f:
            f.write_bytes(data)

    def test_location(self):
        self.assertEqual((store_at("s3://b/notes/hub/").location, store_at("s3://b/notes/hub/").prefix),
                         ("s3://b/notes/hub", "notes/hub/"))
        self.assertEqual((store_at("s3://b").location, store_at("s3://b").prefix), ("s3://b", ""))
        with self.assertRaisesRegex(StoreError, "no bucket"):
            store_at("s3:///notes")

    def test_sig_moves_on_a_rewrite_of_the_same_size(self):
        self.put("m", b"one")
        before = self.store.stat("m").sig
        self.put("m", b"two")
        self.assertNotEqual(self.store.stat("m").sig, before, "LastModified is whole seconds: the ETag must say")

    def test_a_big_file_goes_up_in_parts(self):
        data = os.urandom(9 << 20)
        self.put("big", data)
        with self.store.reading("big") as f:
            self.assertEqual(f.read_bytes(), data)

    def test_clean_aborts_uploads_that_died(self):
        for key in ("hub/a/s/big", "hub/a/s/deeper/big"):
            self.client.create_multipart_upload(Bucket="ferry-test", Key=key)
        self.store.clean("a/s")
        left = self.client.list_multipart_uploads(Bucket="ferry-test").get("Uploads", [])
        self.assertEqual([u["Key"] for u in left], ["hub/a/s/deeper/big"])

    def test_a_missing_bucket_is_an_error_not_a_hub_thats_down(self):
        with self.assertRaisesRegex(StoreError, "no bucket nowhere"):
            Hub(S3Store("nowhere", "hub/"), "alpha", None).is_up()
        with self.assertRaisesRegex(StoreError, "no bucket nowhere"):
            S3Store("nowhere", "hub/").check_vacant()

    def test_vacant(self):
        with self.assertRaisesRegex(StoreError, "isn't empty"):
            self.store.check_vacant()
        self.assertIsNone(S3Store("ferry-test", "other/").check_vacant())

    def test_hub(self):
        alpha, beta = Hub(self.store, "alpha", None), Hub(self.store, "beta", None)
        self.assertFalse(alpha.is_up())
        alpha.create()
        self.assertTrue(alpha.is_up())
        alpha.join()
        beta.join()
        self.assertEqual((alpha.hosts(), alpha.others()), (["alpha", "beta"], ["beta"]))
        self.assertIsNone(alpha.take_lock("source"))
        self.assertIsNone(alpha.take_lock("source"), "left from an earlier attempt")
        self.assertEqual(beta.take_lock("source"), "alpha")
        self.assertTrue(beta.lock_held("source"))
        beta.retire("alpha")
        self.assertEqual((beta.hosts(), beta.lock_held("source")), (["beta"], False))


@unittest.skipIf(S3Store is None, "needs moto, from the dev group: uv sync")
class S3Unreachable(unittest.TestCase):
    def test_a_hub_that_cant_be_reached_is_down(self):
        nowhere = boto3.client("s3", endpoint_url="http://127.0.0.1:9", region_name="us-east-1",
                               aws_access_key_id="x", aws_secret_access_key="x",
                               config=Config(connect_timeout=1, retries={"max_attempts": 1}))
        store = S3Store("ferry-test", "hub/", client=nowhere)
        self.assertFalse(Hub(store, "alpha", None).is_up())
        with self.assertRaises(ConnectionError):
            store.stat("FORMAT")


class HubFiles(unittest.TestCase):
    """What a real share adds: a ._ companion beside every file, and mtimes finer
    than a second."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hub = Hub(FolderStore(self.root), "alpha", None)
        self.slot = self.root / "alpha" / "source"
        self.slot.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, *names):
        for n in names:
            (self.slot / n).write_text(n)

    def test_sweep_leaves_companions_to_the_share(self):
        self.touch("a.pack.age", "._a.pack.age", "b.pack.age", ".c.pack.age.part", "manifest.age")
        self.assertEqual(self.hub.packs("alpha", "source"), {"a.pack.age", "b.pack.age"})
        self.hub.sweep("source", {"a.pack.age"})
        self.assertEqual(sorted(p.name for p in self.slot.iterdir()), ["._a.pack.age", "a.pack.age", "manifest.age"])

    def test_listings_skip_companions(self):
        (self.root / "._alpha").write_text("")
        (self.root / ".compacting-source").mkdir()
        self.assertEqual(self.hub.hosts(), ["alpha"])
        self.hub.retire("alpha")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [".compacting-source"])

    def test_vacant(self):
        self.assertIsNone(FolderStore(self.root / "new").check_vacant())
        with self.assertRaisesRegex(StoreError, "isn't empty"):
            FolderStore(self.root).check_vacant()
        with self.assertRaisesRegex(StoreError, "is the share mounted"):
            FolderStore(self.root / "unmounted" / "hub").check_vacant()

    def test_format(self):
        self.assertFalse(self.hub.is_up(), "no FORMAT: the share isn't mounted")
        (self.root / "FORMAT").write_bytes(b"ferry 9\n")
        with self.assertRaises(HubFormatError):
            self.hub.is_up()

    def test_shared_files_are_lf_on_every_os(self):
        self.hub.create()
        self.assertEqual((self.root / "FORMAT").read_bytes(), b"ferry 1\n")
        self.assertIsNone(self.hub.take_lock("source"))
        self.assertEqual((self.root / ".compacting-source" / "owner").read_bytes(), b"alpha\n")
        self.hub.release_lock("source")
        self.assertFalse(self.hub.lock_held("source"))

    def test_manifest_sig_sees_a_rewrite_within_the_second(self):
        f = self.slot / "manifest.age"
        f.write_text("x")
        os.utime(f, ns=(1_762_000_000_100_000_000,) * 2)
        before = self.hub.manifest_sig("alpha", "source")
        os.utime(f, ns=(1_762_000_000_600_000_000,) * 2)
        self.assertNotEqual(self.hub.manifest_sig("alpha", "source"), before)


class ThisMachine(unittest.TestCase):
    def test_claude_slug(self):
        # Each expected value is from Claude Code's own JavaScript, run under node.
        cases = {
            "/Users/x/Documents/notes": "-Users-x-Documents-notes",
            "C:\\Users\\x\\notes": "C--Users-x-notes",
            "/Users/x/my_notes.v2 copy": "-Users-x-my-notes-v2-copy",
            "C:\\Users\\Zoë\\面试\\😀": "C--Users-Zo-------",
            "/" + "a" * 250: "-" + "a" * 199 + "-feo44x",
            "C:\\" + "Project Notes " * 16:
                "C--" + "Project-Notes-" * 14 + "P-86va2j",
        }
        for path, slug in cases.items():
            self.assertEqual(claude_slug(path), slug, path)

    def test_alive(self):
        me = os.getpid()
        self.assertTrue(system.alive(me))
        self.assertTrue(system.alive(me, system.started(me)))
        if system.started(me):              # Windows and Linux; macOS doesn't say
            self.assertFalse(system.alive(me, "1"), "the same pid, started at another time, is another process")
        gone = int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                                  capture_output=True, text=True, check=True).stdout)
        self.assertFalse(system.alive(gone))

    def test_retry_waits_out_a_held_file(self):
        calls = []

        def held_twice():
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, "in use")
        system.retry(held_twice)
        self.assertEqual(len(calls), 3)
        with self.assertRaises(FileNotFoundError):              # other errors aren't retried
            system.retry(os.unlink, Path(tempfile.gettempdir()) / "ferry-test-no-such-file")


class Layering(unittest.TestCase):
    """Only gitcli runs git, only crypto touches age, only s3 touches AWS, and
    only gitcli and system start processes — so features never mix with command
    lines."""

    def imports(self, module: Path) -> set[str]:
        found = set()
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                found.add(node.module.split(".")[0])
        return found

    def test_only_the_wrappers_reach_outside(self):
        allowed = {"subprocess": {"gitcli.py", "system.py"}, "pyrage": {"crypto.py"},
                   "boto3": {"s3.py"}, "botocore": {"s3.py"}}
        for module in sorted(LIB.glob("*.py")):
            if module.name.startswith("._"):    # macOS's AppleDouble companions, on a copy made on a share
                continue
            for name, owners in allowed.items():
                if name in self.imports(module):
                    self.assertIn(module.name, owners, f"{module.name} imports {name}")

    def test_system_runs_no_git(self):
        self.assertNotIn('"git"', (LIB / "system.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
