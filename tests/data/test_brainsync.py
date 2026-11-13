"""Local git and SQLite proof for brain transport. No live SSH, GitHub, or vault."""

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import brainsync
from tests.data.test_brainsync_cli import _BrainDriver

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-abcdefghijklmnopqrstuvwxyz"
OUTSIDE = "OUTSIDE-SENTINEL-9f3a"
MEMORY_MARK = "plain-memory-row"
CREDENTIAL_MARK = "CREDENTIAL-SENTINEL"


def raw_git(*args, cwd=None, check=True, input=None):
    cmd = ["git", "-c", "protocol.file.allow=always", "-c", "safe.directory=*", *args]
    proc = subprocess.run(cmd, cwd=cwd, input=input, capture_output=True,
                          env=brainsync._git_env(), check=False)
    if check and proc.returncode:
        raise AssertionError(proc.stderr.decode("utf-8", "replace")[-500:])
    return proc


def git_bodies(repo):
    """Every commit, tree, and blob reachable in ``repo``."""
    commits = raw_git("rev-list", "--all", cwd=repo, check=False)
    if commits.returncode:
        return b""
    bodies = []
    for line in commits.stdout.splitlines():
        sha = line.decode().strip()
        bodies.append(raw_git("cat-file", "-p", sha, cwd=repo).stdout)
    listed = raw_git("rev-list", "--objects", "--all", cwd=repo).stdout.splitlines()
    for line in listed:
        parts = line.decode().split(" ", 1)
        kind = raw_git("cat-file", "-t", parts[0], cwd=repo).stdout.decode().strip()
        if kind in ("blob", "tree"):
            bodies.append(raw_git("cat-file", kind, parts[0], cwd=repo).stdout)
    return b"\n".join(bodies)


def devices():
    return {
        "arch": {"hostname": "arch-host", "transport": "ssh", "target": "a@127.0.0.1"},
        "dell": {"hostname": "dell-host", "transport": "ssh", "target": "d@127.0.0.1"},
    }


class BrainSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._driver = _BrainDriver()

    @classmethod
    def tearDownClass(cls):
        cls._driver.close()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def make(self, device, main, files=None, memory=None, remote=None, allow=None, enabled=True,
             repo=None):
        base = self.root / device
        vault = base / "vault"
        vault.mkdir(parents=True)
        state = base / ".jarvis"
        state.mkdir()
        os.chmod(state, 0o700)
        for rel, text in (files or {}).items():
            path = vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text if isinstance(text, bytes) else text.encode())
        if memory is not None:
            con = sqlite3.connect(state / "memory.sqlite3")
            con.execute("CREATE TABLE items(body TEXT)")
            for row in memory:
                con.execute("INSERT INTO items(body) VALUES (?)", (row,))
            con.commit()
            con.close()
        brain = {"enabled": enabled}
        if remote is not None:
            brain["remote"] = str(remote)
        if allow is not None:
            brain["remote_allowlist"] = list(allow)
        if repo is not None:
            brain["repo"] = str(repo)
        config = {"cluster": {"enabled": True, "local_device": device, "main_device": main,
                              "main_url": "http://127.0.0.1:7780"},
                  "devices": devices(), "vault": {"enabled": True, "path": str(vault)},
                  "brainsync": brain}
        return base, config, vault

    def test_disabled_status_is_read_only_and_sync_handoff_refuse(self):
        base = self.root / "off"
        base.mkdir()
        before = sorted(os.listdir(base))
        config = {"brainsync": {"enabled": False}}
        report = brainsync.status(config, base)
        self.assertTrue(report["ok"])
        self.assertFalse(report["enabled"])
        self.assertEqual(report["status"], "disabled")
        self.assertFalse(brainsync.sync(config, base)["ok"])
        refused = brainsync.handoff("arch", "dell", config, base)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], "disabled")
        self.assertEqual(sorted(os.listdir(base)), before)
        self.assertFalse((base / ".jarvis").exists())

    def test_refuses_checkout_and_vault_as_repo(self):
        base, config, vault = self.make("arch", "arch", {"note.md": "keep"}, repo=None)
        raw_git("init", "-b", "main", str(base))
        raw_git("add", "vault/note.md", cwd=base)
        raw_git("commit", "-m", "seed", cwd=base)
        seeded = raw_git("rev-parse", "HEAD", cwd=base).stdout.decode().strip()
        config["brainsync"]["repo"] = str(base)
        refused = brainsync.sync(config, base)
        self.assertEqual(refused["error"], "repo_is_checkout")
        self.assertEqual(raw_git("rev-parse", "HEAD", cwd=base).stdout.decode().strip(), seeded)
        config["brainsync"]["repo"] = str(vault)
        refused = brainsync.sync(config, base)
        self.assertEqual(refused["error"], "repo_is_vault")
        self.assertEqual((vault / "note.md").read_text(), "keep")

    def test_not_main_does_not_create_a_repository(self):
        base, config, _vault = self.make("dell", "arch", {"note.md": "x"})
        result = brainsync.sync(config, base)
        self.assertEqual(result["error"], "not_main")
        self.assertFalse((base / ".jarvis" / "brain-git").exists())

    def test_sync_both_ways_conflict_and_exclusion(self):
        bare = self.root / "remote.git"
        raw_git("init", "--bare", str(bare))
        commands = []
        real = subprocess.run

        def spy(cmd, **kwargs):
            commands.append(list(cmd) if isinstance(cmd, (list, tuple)) else cmd)
            self.assertIsNot(kwargs.get("shell"), True)
            if isinstance(cmd, (list, tuple)) and cmd and "git" in Path(str(cmd[0])).name:
                self.assertNotIn("--force", cmd)
                self.assertNotIn("-f", cmd)
                self.assertNotIn("--autostash", cmd)
                self.assertFalse(any(isinstance(part, str) and part.startswith("+") for part in cmd))
            return real(cmd, **kwargs)

        arch, arch_cfg, arch_vault = self.make(
            "arch", "arch", {"a.md": "from-arch", "credentials.json": CREDENTIAL_MARK},
            memory=[MEMORY_MARK], remote=bare)
        (arch_vault / ".obsidian").mkdir()
        (arch_vault / ".obsidian" / "workspace.json").write_text("OBSIDIAN-PRIVATE")
        (arch_vault / ".env").write_text("TOKEN=not-for-git")
        outside = self.root / "outside.txt"
        outside.write_text(OUTSIDE)
        (arch_vault / "link.md").symlink_to(outside)
        dell, dell_cfg, dell_vault = self.make(
            "dell", "dell", {"b.md": "from-dell"}, remote=bare)
        with patch("subprocess.run", spy):
            first = brainsync.sync(arch_cfg, arch)
            second = brainsync.sync(dell_cfg, dell)
            third = brainsync.sync(arch_cfg, arch)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertTrue(third["ok"], third)
        self.assertEqual((dell_vault / "a.md").read_text(), "from-arch")
        self.assertEqual((arch_vault / "b.md").read_text(), "from-dell")
        self.assertEqual((arch_vault / "a.md").read_text(), "from-arch")
        self.assertFalse((dell_vault / "credentials.json").exists())
        self.assertFalse((dell_vault / ".env").exists())
        self.assertFalse((dell_vault / "link.md").exists())
        self.assertFalse((dell_vault / ".obsidian").exists())
        bare_body = git_bodies(bare)
        self.assertNotIn(CREDENTIAL_MARK.encode(), bare_body)
        self.assertNotIn(b"OBSIDIAN-PRIVATE", bare_body)
        self.assertNotIn(OUTSIDE.encode(), bare_body)
        self.assertNotIn(MEMORY_MARK.encode(), bare_body)
        self.assertNotIn(b"TOKEN=not-for-git", bare_body)
        self.assertIn(b"from-arch", bare_body)
        self.assertIn(b"from-dell", bare_body)
        refs = raw_git("ls-remote", "--heads", str(bare)).stdout.decode()
        self.assertIn("refs/heads/brainsync/arch", refs)
        self.assertIn("refs/heads/brainsync/dell", refs)

    def test_conflict_copy_keeps_both_bytes(self):
        bare = self.root / "conflict.git"
        raw_git("init", "--bare", str(bare))
        arch, arch_cfg, arch_vault = self.make("arch", "arch", {"note.md": "alpha"}, remote=bare)
        dell, dell_cfg, dell_vault = self.make("dell", "dell", {"note.md": "beta"}, remote=bare)
        self.assertTrue(brainsync.sync(arch_cfg, arch)["ok"])
        got = brainsync.sync(dell_cfg, dell)
        self.assertTrue(got["ok"], got)
        self.assertEqual((dell_vault / "note.md").read_bytes(), b"beta")
        side = dell_vault / f"conflict-{hashlib.sha256(b'alpha').hexdigest()}.md"
        self.assertEqual(side.read_bytes(), b"alpha")
        self.assertTrue(brainsync.sync(arch_cfg, arch)["ok"])
        self.assertEqual((arch_vault / "note.md").read_bytes(), b"alpha")
        other = arch_vault / f"conflict-{hashlib.sha256(b'beta').hexdigest()}.md"
        self.assertEqual(other.read_bytes(), b"beta")

    def test_old_secret_history_is_not_pushed_and_deletions_remain(self):
        bare = self.root / "history.git"
        raw_git("init", "--bare", str(bare))
        arch, arch_cfg, arch_vault = self.make(
            "arch", "arch", {"keep.md": "kept", "drop.md": "still-here"}, remote=bare)
        self.assertTrue(brainsync.sync(arch_cfg, arch)["ok"])
        repo = arch / ".jarvis" / "brain-git"
        leak = repo / "vault" / "leak.txt"
        leak.parent.mkdir(parents=True, exist_ok=True)
        leak.write_text(SECRET)
        brainsync._git(repo, "add", "--", "vault/leak.txt")
        tree = brainsync._git(repo, "write-tree").stdout.decode().strip()
        parent = brainsync._rev(repo, "refs/heads/brainsync/arch")
        child = brainsync._git(repo, "commit-tree", tree, "-p", parent, input=b"poison\n")
        brainsync._git(repo, "update-ref", "refs/heads/brainsync/arch",
                       child.stdout.decode().strip(), parent)
        (arch_vault / "drop.md").unlink()
        again = brainsync.sync(arch_cfg, arch)
        self.assertTrue(again["ok"], again)
        bare_body = git_bodies(bare)
        self.assertNotIn(SECRET.encode(), bare_body)
        self.assertIn(b"still-here", bare_body)
        self.assertEqual((arch_vault / "drop.md").read_text(), "still-here")
        dell, dell_cfg, dell_vault = self.make("dell", "dell", remote=bare)
        peer = brainsync.sync(dell_cfg, dell)
        self.assertTrue(peer["ok"], peer)
        self.assertEqual((dell_vault / "keep.md").read_text(), "kept")
        self.assertEqual((dell_vault / "drop.md").read_text(), "still-here")
        self.assertFalse((dell_vault / "leak.txt").exists())
        self.assertNotIn(SECRET.encode(), git_bodies(dell / ".jarvis" / "brain-git"))
        self.assertFalse(any(SECRET in path.read_text(errors="ignore")
                             for path in arch_vault.rglob("*") if path.is_file()))

    def test_divergent_histories_are_not_force_pushed(self):
        bare = self.root / "diverge.git"
        raw_git("init", "--bare", str(bare))
        seed = self.root / "seed"
        (seed / "vault").mkdir(parents=True)
        (seed / "vault" / "seed.md").write_text("from-seed")
        raw_git("init", "-b", "brainsync/arch", str(seed))
        raw_git("add", "vault/seed.md", cwd=seed)
        raw_git("commit", "-m", "seed", cwd=seed)
        raw_git("push", str(bare), "HEAD:refs/heads/brainsync/arch", cwd=seed)
        seeded = raw_git("rev-parse", "HEAD", cwd=seed).stdout.decode().strip()
        arch, arch_cfg, arch_vault = self.make("arch", "arch", {"ours.md": "from-arch"}, remote=bare)
        result = brainsync.sync(arch_cfg, arch)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["pushed"])
        self.assertTrue(result["preserved"])
        remote_sha = raw_git("rev-parse", "refs/heads/brainsync/arch", cwd=bare).stdout.decode().strip()
        self.assertEqual(remote_sha, seeded)
        local_sha = brainsync._rev(arch / ".jarvis" / "brain-git", "refs/heads/brainsync/arch")
        self.assertNotEqual(local_sha, seeded)
        self.assertEqual((arch_vault / "ours.md").read_text(), "from-arch")
        self.assertEqual((arch_vault / "seed.md").read_text(), "from-seed")

    def test_wal_snapshot_is_not_a_raw_copy(self):
        base, config, _vault = self.make("arch", "arch", {"note.md": "x"})
        db = base / ".jarvis" / "memory.sqlite3"
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("CREATE TABLE items(body TEXT)")
        con.execute("INSERT INTO items(body) VALUES ('durable')")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("INSERT INTO items(body) VALUES ('wal-only')")
        con.commit()
        self.assertTrue((base / ".jarvis" / "memory.sqlite3-wal").stat().st_size > 0)
        raw = self.root / "raw-copy.sqlite3"
        shutil.copyfile(db, raw)
        expected = brainsync.snapshot_memory(db)
        try:
            raw_con = sqlite3.connect(raw)
            try:
                raw_rows = [row[0] for row in raw_con.execute("SELECT body FROM items")]
            finally:
                raw_con.close()
        except sqlite3.DatabaseError:
            raw_rows = []
        self.assertNotIn("wal-only", raw_rows)
        snap = self.root / "snap.sqlite3"
        snap.write_bytes(expected)
        snap_con = sqlite3.connect(snap)
        try:
            snap_rows = [row[0] for row in snap_con.execute("SELECT body FROM items")]
        finally:
            snap_con.close()
        self.assertEqual(snap_rows, ["durable", "wal-only"])
        result = brainsync.sync(config, base)
        self.assertTrue(result["ok"], result)
        staged = base / ".jarvis" / "brain-git" / "state" / "memory.sqlite3"
        self.assertEqual(staged.read_bytes(), brainsync.snapshot_memory(db))
        con.close()

    def test_allowlisted_memory_can_be_pushed(self):
        bare = self.root / "memory.git"
        raw_git("init", "--bare", str(bare))
        base, config, _vault = self.make(
            "arch", "arch", {"note.md": "x"}, memory=[MEMORY_MARK], remote=bare,
            allow=["state/memory.sqlite3"])
        result = brainsync.sync(config, base)
        self.assertTrue(result["ok"], result)
        self.assertIn(MEMORY_MARK.encode(), git_bodies(bare))

    def test_structural_paths_never_escape(self):
        self.assertFalse(brainsync._structural("vault/../../etc/passwd"))
        self.assertFalse(brainsync._structural("vault/.git/config"))
        self.assertFalse(brainsync._structural("vault/.obsidian/workspace.json"))
        self.assertFalse(brainsync._structural(".jarvis/memory.sqlite3"))
        self.assertFalse(brainsync._structural("vault/../note.md"))

    def test_direct_bundle_and_production_ssh_adapter(self):
        arch, arch_cfg, arch_vault = self.make("arch", "arch", {"moved.md": "from-old"},
                                              memory=["source-brain"])
        dell, dell_cfg, dell_vault = self.make("dell", "arch", {"keep.md": "local"})
        request = json.dumps({"schema": brainsync.EXPORT_SCHEMA, "old_main": "arch",
                              "new_main": "dell"}).encode()
        exported = brainsync.export_result(arch_cfg, arch, request)
        self.assertTrue(exported["ok"], exported)
        blob = base64.b64decode("".join(exported["payload"].split()), validate=True)
        self.assertTrue(blob.startswith(brainsync._MAGIC))
        # Reset the destination and replay the same bytes through the SSH adapter.
        shutil.rmtree(dell / ".jarvis" / "brain-git", ignore_errors=True)
        (dell_vault / "moved.md").unlink(missing_ok=True)
        seen = []

        def remote(_config, _state, node, command, stdin=b"", timeout=120):
            self.assertIn(command, brainsync._FIXED_COMMANDS)
            self.assertNotIn(node, command)
            self.assertNotIn("{", command)
            seen.append(command)
            if command == brainsync.EXPORT_COMMAND:
                result = brainsync.export_result(arch_cfg, arch, stdin)
                self.assertTrue(result["ok"], result)
                return 0, result["payload"], ""
            if command == brainsync.RECEIVE_COMMAND:
                self.assertTrue(bytes(stdin).startswith(brainsync._MAGIC))
                result = brainsync.receive(dell_cfg, dell, stdin)
                return (0 if result["ok"] else 1), json.dumps(result), ""
            result = brainsync.freeze(arch_cfg, arch, stdin)
            return (0 if result["ok"] else 1), json.dumps(result), ""

        with patch("jarvis.deploy._remote", remote):
            moved = brainsync.handoff("arch", "dell", dell_cfg, dell)
            self.assertTrue(moved["ok"], moved)
            # Pull imports locally. The old MAIN's production push is what puts the
            # raw bundle on `brain receive` stdin. Replay that path with the export.
            replay = brainsync.SshBundleTransport(arch_cfg, arch).push("dell", blob)
            self.assertTrue(replay["ok"], replay)
        self.assertEqual(moved["status"], "handed_off")
        self.assertEqual(set(seen), set(brainsync._FIXED_COMMANDS))
        self.assertEqual((dell_vault / "moved.md").read_text(), "from-old")
        self.assertEqual((dell_vault / "keep.md").read_text(), "local")
        mem = sqlite3.connect(dell / ".jarvis" / "memory.sqlite3")
        try:
            rows = [row[0] for row in mem.execute("SELECT body FROM items")]
        finally:
            mem.close()
        self.assertEqual(rows, ["source-brain"])
        marker = json.loads((arch / ".jarvis" / "brainsync-handoff.json").read_text())
        self.assertTrue(marker["readonly"])
        self.assertEqual(marker["old_main"], "arch")
        (arch_vault / "later.md").write_text("must-not-publish")
        blocked = brainsync.sync(arch_cfg, arch)
        self.assertEqual(blocked["error"], "handoff_readonly")
        self.assertFalse((arch / ".jarvis" / "brain-git" / "vault" / "later.md").exists())

    def test_offline_source_does_not_claim_latest(self):
        arch, _arch_cfg, _arch_vault = self.make("arch", "arch", {"moved.md": "from-old"},
                                                memory=["source-brain"])
        dell, dell_cfg, dell_vault = self.make("dell", "arch", {"keep.md": "local"},
                                              memory=["dest-brain"])
        before = (dell / ".jarvis" / "memory.sqlite3").read_bytes()

        def offline(*_args, **_kwargs):
            raise ConnectionError("no address answered")

        with patch("jarvis.deploy._remote", offline):
            result = brainsync.handoff("arch", "dell", dell_cfg, dell)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "source_offline")
        self.assertNotIn("latest", json.dumps(result))
        self.assertEqual((dell / ".jarvis" / "memory.sqlite3").read_bytes(), before)
        self.assertEqual((dell_vault / "keep.md").read_text(), "local")
        self.assertFalse((dell_vault / "moved.md").exists())
        self.assertFalse(list((dell / ".jarvis").glob("memory.sqlite3.conflict-*")))
        self.assertFalse((arch / ".jarvis" / "brainsync-handoff.json").exists())

    def test_quiesce_failure_and_memory_conflict_keep_both_databases(self):
        arch, arch_cfg, _arch_vault = self.make("arch", "arch", {"note.md": "x"}, memory=["source-brain"])
        dell, dell_cfg, _dell_vault = self.make("dell", "arch", {"note.md": "y"}, memory=["dest-brain"])
        db = arch / ".jarvis" / "memory.sqlite3"

        def mutate():
            con = sqlite3.connect(db)
            con.execute("INSERT INTO items(body) VALUES ('race')")
            con.commit()
            con.close()

        class Boom:
            def push(self, *_args, **_kwargs):
                raise AssertionError("transport used")

            def pull(self, *_args, **_kwargs):
                raise AssertionError("transport used")

            def freeze(self, *_args, **_kwargs):
                raise AssertionError("transport used")

        quiet = brainsync.handoff("arch", "dell", arch_cfg, arch, transport=Boom(), between_reads=mutate)
        self.assertEqual(quiet["error"], "source_not_quiesced")
        self.assertNotIn("latest", json.dumps(quiet))
        # The race row is local to the source. Rebuild a stable pair for the conflict.
        shutil.rmtree(arch / ".jarvis" / "brain-git", ignore_errors=True)
        for stale in ("brainsync-pending.json", "brainsync-pending.bin"):
            (arch / ".jarvis" / stale).unlink(missing_ok=True)
        seen = []

        def remote(_config, _state, node, command, stdin=b"", timeout=120):
            seen.append(command)
            if command == brainsync.EXPORT_COMMAND:
                result = brainsync.export_result(arch_cfg, arch, stdin)
                return (0 if result["ok"] else 1), result.get("payload") or "", ""
            if command == brainsync.RECEIVE_COMMAND:
                result = brainsync.receive(dell_cfg, dell, stdin)
                return (0 if result["ok"] else 1), json.dumps(result), ""
            result = brainsync.freeze(arch_cfg, arch, stdin)
            return (0 if result["ok"] else 1), json.dumps(result), ""

        before = (dell / ".jarvis" / "memory.sqlite3").read_bytes()
        with patch("jarvis.deploy._remote", remote):
            conflict = brainsync.handoff("arch", "dell", dell_cfg, dell)
        self.assertEqual(conflict["error"], "memory_conflict")
        self.assertFalse(conflict["ok"])
        self.assertNotIn("latest", json.dumps(conflict))
        self.assertEqual((dell / ".jarvis" / "memory.sqlite3").read_bytes(), before)
        sides = list((dell / ".jarvis").glob("memory.sqlite3.conflict-*"))
        self.assertEqual(len(sides), 1)
        side_con = sqlite3.connect(sides[0])
        try:
            rows = [row[0] for row in side_con.execute("SELECT body FROM items")]
        finally:
            side_con.close()
        self.assertIn("source-brain", rows)
        self.assertNotIn(brainsync.FREEZE_COMMAND, seen)
        source_con = sqlite3.connect(db)
        try:
            source_rows = [row[0] for row in source_con.execute("SELECT body FROM items")]
        finally:
            source_con.close()
        self.assertIn("source-brain", source_rows)

    def test_cli_status_and_hidden_receive(self):
        base = self.root / "cli"
        base.mkdir()
        config_path = base / "jarvis.config.json"
        config_path.write_text("{}\n")
        state = base / ".jarvis"
        prefix = ["--config", str(config_path), "--state", str(state), "brain"]
        help_proc = self._driver.run([*prefix, "-h"], ROOT, None, 30)
        self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
        help_text = help_proc.stdout.decode()
        self.assertNotIn("receive", help_text)
        self.assertNotIn("export", help_text)
        self.assertNotIn("freeze", help_text)
        status = self._driver.run([*prefix, "status"], ROOT, None, 30)
        self.assertEqual(status.returncode, 0, status.stderr)
        body = json.loads(status.stdout.decode())
        self.assertTrue(body["ok"])
        self.assertFalse(body["enabled"])
        hidden = self._driver.run([*prefix, "receive"], ROOT, b"not-a-bundle", 30)
        self.assertNotEqual(hidden.returncode, 0)
        self.assertFalse((base / ".jarvis" / "brain-git").exists())


if __name__ == "__main__":
    unittest.main()
