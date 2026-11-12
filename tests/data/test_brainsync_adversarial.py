"""Adversarial brain transport. Real local git only: no SSH, live vault, or network.

Expectations stay strict where the current transport is wrong. A refusal must not
be weakened into a successful handoff, and neither side's bytes are discarded.
"""

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import brainsync


FRESH_STATUSES = frozenset({"handed_off", "latest", "current", "synced", "imported"})
PRIOR_MAIN = "PRIOR-MAIN-PRIVATE-7f3c\nsk-prior-main-private-7f3cABCD\n"
FORMER_MAIN = "FORMER-MAIN-PRIVATE-7f3c\nsk-former-main-private-7f3cXX\n"
LOCAL_MEMORY = "LOCAL-REF-MEMORY-7f3c"


def raw_git(*args, cwd=None, check=True, input=None):
    cmd = ["git", "-c", "protocol.file.allow=always", "-c", "safe.directory=*", *args]
    proc = subprocess.run(cmd, cwd=cwd, input=input, capture_output=True,
                          env=brainsync._git_env(), check=False)
    if check and proc.returncode:
        raise AssertionError(proc.stderr.decode("utf-8", "replace")[-800:])
    return proc


def devices():
    return {
        "arch": {"hostname": "arch-host", "transport": "ssh", "target": "a@127.0.0.1"},
        "dell": {"hostname": "dell-host", "transport": "ssh", "target": "d@127.0.0.1"},
    }


def object_shas(repo, ref):
    """Every object reachable from ``ref`` (commits, trees, and blobs)."""
    listed = raw_git("rev-list", "--objects", ref, cwd=repo)
    found = []
    for line in listed.stdout.splitlines():
        sha = line.decode("ascii", "replace").split(" ", 1)[0].strip()
        if len(sha) == 40:
            found.append(sha)
    return found


def odb_shas(repo):
    """Every object in the database, including ones no ref points at."""
    listed = raw_git("cat-file", "--batch-check", "--batch-all-objects", cwd=repo, check=False)
    if listed.returncode:
        raise AssertionError(listed.stderr.decode("utf-8", "replace")[-800:])
    found = set()
    for line in listed.stdout.splitlines():
        sha = line.decode("ascii", "replace").split(" ", 1)[0].strip()
        if len(sha) == 40:
            found.add(sha)
    return found


def odb_bytes(repo):
    body = []
    for sha in sorted(odb_shas(repo)):
        body.append(raw_git("cat-file", "-p", sha, cwd=repo).stdout)
    return b"\n".join(body)


class AdversarialBrainSyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self._seq = 0

    def make(self, label, device, main, files=None, memory=None, remote=None, allow=None):
        base = self.root / label / device
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
        brain = {"enabled": True}
        if remote is not None:
            brain["remote"] = str(remote)
        if allow is not None:
            brain["remote_allowlist"] = list(allow)
        config = {"cluster": {"enabled": True, "local_device": device, "main_device": main,
                              "main_url": "http://127.0.0.1:7780"},
                  "devices": devices(), "vault": {"enabled": True, "path": str(vault)},
                  "brainsync": brain}
        return base, config, vault

    def pair(self, label, *, arch_files, dell_files, arch_memory=None, dell_memory=None):
        arch, arch_cfg, arch_vault = self.make(label, "arch", "arch", arch_files, arch_memory)
        dell, dell_cfg, dell_vault = self.make(label, "dell", "arch", dell_files, dell_memory)
        return arch, arch_cfg, arch_vault, dell, dell_cfg, dell_vault

    def rows(self, path):
        path = Path(path)
        if not path.exists():
            return None
        con = sqlite3.connect(path)
        try:
            return [row[0] for row in con.execute("SELECT body FROM items ORDER BY rowid")]
        finally:
            con.close()

    def pull_handoff(self, arch, arch_cfg, dell, dell_cfg, *, after_export=None):
        """In-process ``SshBundleTransport`` stand-in. Export, receive, and freeze are real."""

        def remote(_config, _state, node, command, stdin=b"", timeout=120):
            self.assertIn(command, brainsync._FIXED_COMMANDS)
            self.assertNotIn(node, command)
            self.assertNotIn("{", command)
            if command == brainsync.EXPORT_COMMAND:
                result = brainsync.export_result(arch_cfg, arch, stdin)
                if after_export is not None:
                    after_export()
                if not result.get("ok"):
                    return 1, json.dumps(result), ""
                return 0, result["payload"], ""
            if command == brainsync.RECEIVE_COMMAND:
                result = brainsync.receive(dell_cfg, dell, stdin)
                return (0 if result.get("ok") else 1), json.dumps(result), ""
            result = brainsync.freeze(arch_cfg, arch, stdin)
            return (0 if result.get("ok") else 1), json.dumps(result), ""

        with patch("jarvis.deploy._remote", remote):
            return brainsync.handoff("arch", "dell", dell_cfg, dell)

    def prove_handoff_harness(self):
        """Positive control: a real handoff keeps the source and the destination's own note."""
        self._seq += 1
        arch, arch_cfg, arch_vault, dell, dell_cfg, dell_vault = self.pair(
            f"prove-{self._seq}",
            arch_files={"moved.md": "from-old"},
            dell_files={"keep.md": "local"},
            arch_memory=["source-brain"])
        result = self.pull_handoff(arch, arch_cfg, dell, dell_cfg)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "handed_off")
        self.assertEqual((arch_vault / "moved.md").read_text(), "from-old")
        self.assertEqual((dell_vault / "moved.md").read_text(), "from-old")
        self.assertEqual((dell_vault / "keep.md").read_text(), "local")
        self.assertEqual(self.rows(arch / ".jarvis" / "memory.sqlite3"), ["source-brain"])
        self.assertEqual(self.rows(dell / ".jarvis" / "memory.sqlite3"), ["source-brain"])
        marker = json.loads((arch / ".jarvis" / "brainsync-handoff.json").read_text())
        self.assertTrue(marker["readonly"])
        self.assertEqual(marker["old_main"], "arch")
        self.assertEqual(marker["new_main"], "dell")
        self.assertNotIn("latest", json.dumps(result))

    def assert_not_fresh(self, result):
        text = json.dumps(result)
        self.assertFalse(result.get("ok"), result)
        self.assertNotIn(result.get("status"), FRESH_STATUSES, result)
        self.assertNotIn("latest", text)
        self.assertNotIn("current", text)

    def export_bundle(self, config, base, old_main="arch", new_main="dell"):
        request = json.dumps({"schema": brainsync.EXPORT_SCHEMA, "old_main": old_main,
                              "new_main": new_main}).encode()
        exported = brainsync.export_result(config, base, request)
        self.assertTrue(exported["ok"], exported)
        blob = base64.b64decode("".join(exported["payload"].split()), validate=True)
        self.assertTrue(blob.startswith(brainsync._MAGIC))
        return blob

    def test_missing_old_main_memory_refuses_handoff(self):
        """Missing ``memory.sqlite3`` is not an implicit empty brain.

        An explicit opt-in could allow a new empty database later. The default
        path must refuse, and must not call the destination current or latest.
        """
        with self.subTest("positive control"):
            self.prove_handoff_harness()
        with self.subTest("missing memory"):
            arch, arch_cfg, arch_vault, dell, dell_cfg, dell_vault = self.pair(
                "missing",
                arch_files={"note.md": "from-old"},
                dell_files={"keep.md": "local"},
                dell_memory=["dest-live"])
            source_db = arch / ".jarvis" / "memory.sqlite3"
            dest_db = dell / ".jarvis" / "memory.sqlite3"
            self.assertFalse(source_db.exists())
            before = dest_db.read_bytes()
            result = self.pull_handoff(arch, arch_cfg, dell, dell_cfg)
            evidence = {
                "result": result,
                "marker": (arch / ".jarvis" / "brainsync-handoff.json").exists(),
                "dest_rows": self.rows(dest_db),
                "dest_note": (dell_vault / "note.md").exists(),
                "source_note": (arch_vault / "note.md").read_text(),
                "dest_keep": (dell_vault / "keep.md").read_text(),
            }
            self.assertEqual(dest_db.read_bytes(), before, evidence)
            self.assertEqual(self.rows(dest_db), ["dest-live"], evidence)
            self.assertEqual((arch_vault / "note.md").read_text(), "from-old", evidence)
            self.assertEqual((dell_vault / "keep.md").read_text(), "local", evidence)
            self.assertFalse((dell_vault / "note.md").exists(), evidence)
            self.assertFalse((arch / ".jarvis" / "brainsync-handoff.json").exists(), evidence)
            self.assert_not_fresh(result)

    def test_oversized_note_cannot_succeed_while_omitted(self):
        with self.subTest("positive control"):
            self.prove_handoff_harness()
        with self.subTest("oversized note"):
            huge = b"HUGE-NOTE-OMITTED-" + (b"Q" * 64)
            arch, arch_cfg, arch_vault, dell, dell_cfg, dell_vault = self.pair(
                "huge",
                arch_files={"note.md": "small-ok", "huge.md": huge},
                dell_files={"keep.md": "local"},
                arch_memory=["source-brain"])
            with patch.object(brainsync, "MAX_FILE_BYTES", 48):
                self.assertGreater(len(huge), brainsync.MAX_FILE_BYTES)
                result = self.pull_handoff(arch, arch_cfg, dell, dell_cfg)
            dest_huge = dell_vault / "huge.md"
            present = dest_huge.is_file() and not dest_huge.is_symlink() and dest_huge.read_bytes() == huge
            omitted = not present
            evidence = {
                "result": result,
                "omitted": omitted,
                "dest_size": dest_huge.stat().st_size if dest_huge.exists() else None,
                "small_arrived": (dell_vault / "note.md").exists(),
            }
            self.assertEqual((arch_vault / "huge.md").read_bytes(), huge, evidence)
            self.assertEqual((arch_vault / "note.md").read_text(), "small-ok", evidence)
            self.assertEqual(self.rows(arch / ".jarvis" / "memory.sqlite3"), ["source-brain"], evidence)
            self.assertEqual((dell_vault / "keep.md").read_text(), "local", evidence)
            if dest_huge.exists():
                self.assertFalse(dest_huge.is_symlink(), evidence)
                self.assertEqual(dest_huge.read_bytes(), huge, evidence)
            self.assertFalse(result.get("ok") and omitted, evidence)
            self.assertFalse(result.get("status") in FRESH_STATUSES and omitted, evidence)

    def test_symlink_collision_cannot_complete_handoff(self):
        with self.subTest("positive control"):
            self.prove_handoff_harness()
        with self.subTest("symlink collision"):
            arch, arch_cfg, arch_vault, dell, dell_cfg, dell_vault = self.pair(
                "link",
                arch_files={"note.md": "from-old", "also.md": "also-from-old"},
                dell_files={"keep.md": "local"},
                arch_memory=["source-brain"])
            outside = self.root / "link" / "outside.txt"
            outside.write_text("OUTSIDE-SYMLINK-TARGET")
            link = dell_vault / "note.md"
            link.symlink_to(outside)
            result = self.pull_handoff(arch, arch_cfg, dell, dell_cfg)
            evidence = {
                "result": result,
                "still_symlink": link.is_symlink(),
                "outside": outside.read_text(),
                "also_copied": (dell_vault / "also.md").exists(),
                "source_note": (arch_vault / "note.md").read_text(),
            }
            self.assertEqual(outside.read_text(), "OUTSIDE-SYMLINK-TARGET", evidence)
            self.assertEqual((arch_vault / "note.md").read_text(), "from-old", evidence)
            self.assertEqual((arch_vault / "also.md").read_text(), "also-from-old", evidence)
            self.assertEqual(self.rows(arch / ".jarvis" / "memory.sqlite3"), ["source-brain"], evidence)
            self.assertEqual((dell_vault / "keep.md").read_text(), "local", evidence)
            if link.is_symlink():
                self.assertEqual(os.readlink(link), str(outside), evidence)
                self.assert_not_fresh(result)

    def test_replayed_bundle_cannot_overwrite_or_claim_fresh(self):
        arch, arch_cfg, arch_vault, dell, dell_cfg, dell_vault = self.pair(
            "replay",
            arch_files={"note.md": "alpha-note"},
            dell_files={"keep.md": "local"},
            arch_memory=["alpha"])
        bundle_a = self.export_bundle(arch_cfg, arch)
        (arch_vault / "note.md").write_text("beta-note")
        source_db = arch / ".jarvis" / "memory.sqlite3"
        con = sqlite3.connect(source_db)
        con.execute("INSERT INTO items(body) VALUES ('beta')")
        con.commit()
        con.close()
        bundle_b = self.export_bundle(arch_cfg, arch)
        self.assertNotEqual(bundle_a, bundle_b)
        imported = brainsync.receive(dell_cfg, dell, bundle_b)
        self.assertTrue(imported["ok"], imported)
        digest_b = hashlib.sha256(bundle_b).hexdigest()
        frozen = brainsync.freeze(arch_cfg, arch, json.dumps({
            "schema": brainsync.EXPORT_SCHEMA, "old_main": "arch", "new_main": "dell",
            "bundle_sha256": digest_b}).encode())
        self.assertTrue(frozen["ok"], frozen)
        dest_db = dell / ".jarvis" / "memory.sqlite3"
        before_db = dest_db.read_bytes()
        before_note = (dell_vault / "note.md").read_bytes()
        self.assertEqual(self.rows(dest_db), ["alpha", "beta"])
        self.assertEqual(before_note, b"beta-note")

        class Replay:
            def push(self, *_args, **_kwargs):
                raise AssertionError("push")

            def pull(self, *_args, **_kwargs):
                return {"ok": True, "status": "exported", "bundle": bundle_a}

            def freeze(self, _node, request):
                return brainsync.freeze(arch_cfg, arch, request)

        replayed = brainsync.handoff("arch", "dell", dell_cfg, dell, transport=Replay())
        evidence = {"result": replayed, "dest_rows": self.rows(dest_db),
                    "dest_note": (dell_vault / "note.md").read_bytes()}
        self.assertEqual(dest_db.read_bytes(), before_db, evidence)
        self.assertEqual((dell_vault / "note.md").read_bytes(), before_note, evidence)
        self.assertEqual((dell_vault / "keep.md").read_text(), "local", evidence)
        self.assertEqual(self.rows(source_db), ["alpha", "beta"], evidence)
        self.assertEqual((arch_vault / "note.md").read_text(), "beta-note", evidence)
        marker = json.loads((arch / ".jarvis" / "brainsync-handoff.json").read_text())
        self.assertEqual(marker["bundle_sha256"], digest_b, evidence)
        self.assert_not_fresh(replayed)

        with self.subTest("source diverges after the bundle is cut"):
            arch2, arch2_cfg, arch2_vault, dell2, dell2_cfg, dell2_vault = self.pair(
                "diverge",
                arch_files={"note.md": "alpha-note"},
                dell_files={"keep.md": "local"},
                arch_memory=["alpha"])
            source2 = arch2 / ".jarvis" / "memory.sqlite3"

            def diverge():
                con2 = sqlite3.connect(source2)
                con2.execute("INSERT INTO items(body) VALUES ('mutated-after-export')")
                con2.commit()
                con2.close()
                (arch2_vault / "note.md").write_text("mutated-note")

            result = self.pull_handoff(arch2, arch2_cfg, dell2, dell2_cfg, after_export=diverge)
            marker2 = arch2 / ".jarvis" / "brainsync-handoff.json"
            evidence = {
                "result": result,
                "marker": marker2.read_text() if marker2.exists() else None,
                "source_rows": self.rows(source2),
                "source_note": (arch2_vault / "note.md").read_text(),
                "dest_rows": self.rows(dell2 / ".jarvis" / "memory.sqlite3"),
                "dest_note": (dell2_vault / "note.md").read_text()
                if (dell2_vault / "note.md").exists() else None,
                "dest_keep": (dell2_vault / "keep.md").read_text(),
            }
            self.assertEqual(self.rows(source2), ["alpha", "mutated-after-export"], evidence)
            self.assertEqual((arch2_vault / "note.md").read_text(), "mutated-note", evidence)
            self.assertEqual((dell2_vault / "keep.md").read_text(), "local", evidence)
            self.assertFalse(marker2.exists(), evidence)
            self.assert_not_fresh(result)

    def test_prior_main_private_refs_are_not_transmitted(self):
        bare = self.root / "private.git"
        raw_git("init", "--bare", str(bare))
        arch, arch_cfg, arch_vault = self.make(
            "remote", "arch", "arch", {"public.md": "public-from-arch"},
            memory=[LOCAL_MEMORY], remote=bare)
        first = brainsync.sync(arch_cfg, arch)
        self.assertTrue(first["ok"], first)
        repo = arch / ".jarvis" / "brain-git"
        self.assertIsNotNone(brainsync._rev(repo, "refs/heads/brainsync-local/arch"))

        def orphan(ref, payload, path):
            blob = brainsync._hash_object(repo, payload.encode())
            parent, name = path.rsplit("/", 1)
            leaf = raw_git("mktree", cwd=repo, input=f"100644 blob {blob}\t{name}\n".encode())
            leaf_sha = leaf.stdout.decode().strip()
            tree = raw_git("mktree", cwd=repo, input=f"040000 tree {leaf_sha}\t{parent}\n".encode())
            commit = raw_git("commit-tree", tree.stdout.decode().strip(), cwd=repo,
                             input=b"prior main private\n")
            raw_git("update-ref", ref, commit.stdout.decode().strip(), cwd=repo)
            return blob

        prior_blob = orphan("refs/heads/main", PRIOR_MAIN, "vault/prior-private.md")
        former_blob = orphan("refs/heads/brainsync-local/former-main", FORMER_MAIN,
                             "state/former-memory.txt")
        (arch_vault / "again.md").write_text("second-sync-public")
        second = brainsync.sync(arch_cfg, arch)
        self.assertTrue(second["ok"], second)

        remote_body = odb_bytes(bare)
        remote_ids = odb_shas(bare)
        refs = raw_git("show-ref", cwd=bare).stdout.decode()
        self.assertIn(b"public-from-arch", remote_body)
        self.assertIn(b"second-sync-public", remote_body)
        self.assertNotIn(b"PRIOR-MAIN-PRIVATE-7f3c", remote_body)
        self.assertNotIn(b"FORMER-MAIN-PRIVATE-7f3c", remote_body)
        self.assertNotIn(b"sk-prior-main-private-7f3cABCD", remote_body)
        self.assertNotIn(b"sk-former-main-private-7f3cXX", remote_body)
        self.assertNotIn(LOCAL_MEMORY.encode(), remote_body)
        self.assertNotIn("refs/heads/main", refs.split())
        self.assertNotIn("refs/heads/brainsync-local/arch", refs.split())
        self.assertNotIn("refs/heads/brainsync-local/former-main", refs.split())
        self.assertNotIn(prior_blob, remote_ids)
        self.assertNotIn(former_blob, remote_ids)

        publish_refs = ["refs/heads/brainsync/arch"]
        sanitized = "refs/heads/brainsync/arch-sanitized"
        if brainsync._rev(repo, sanitized):
            publish_refs.append(sanitized)
        published = set()
        for ref in publish_refs:
            published.update(object_shas(repo, ref))
        private_refs = ("refs/heads/main", "refs/heads/brainsync-local/arch",
                        "refs/heads/brainsync-local/former-main")
        private_only = set()
        for ref in private_refs:
            self.assertIsNotNone(brainsync._rev(repo, ref), ref)
            private_only.update(object_shas(repo, ref))
        private_only -= published
        self.assertTrue(private_only)
        self.assertIn(prior_blob, private_only)
        self.assertIn(former_blob, private_only)
        leaked = sorted(private_only & remote_ids)
        self.assertEqual(leaked, [], leaked)


if __name__ == "__main__":
    unittest.main()
