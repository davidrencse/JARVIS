"""`jarvis archive ...` (jarvis/second_brain_runtime.cli + __main__ wiring) on a fake vault and a
fake state dir in a temp folder. No real vault, state, model, service or quota is touched."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("JARVIS_NO_MODELS", "1")

from jarvis import __main__ as entry  # noqa: E402
from jarvis import second_brain_runtime as rt  # noqa: E402
from tests.data.test_second_brain import make_state  # noqa: E402

SECRET_TEXT = "Alpha body"   # personal text from the fixture: must never reach CLI output


def ns(action, **kw):
    base = {"action": action, "apply": False, "limit": 100, "target": None}
    base.update(kw)
    return SimpleNamespace(**base)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.vault, self.state = self.tmp / "vault", self.tmp / "state"
        (self.vault / ".obsidian").mkdir(parents=True)
        make_state(self.state)
        self.config = {"vault": {"enabled": True, "path": str(self.vault), "write": False}}

    def tree(self, root):
        return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))

    def run_cli(self, action, config=None, queue=None, **kw):
        return rt.cli(ns(action, **kw), config or self.config, self.state, queue=queue)


class SyncValidateRestore(Base):
    def test_sync_is_dry_run_by_default(self):
        before = self.tree(self.vault)
        out, code = self.run_cli("sync")
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"] and out["ok"])
        self.assertEqual(self.tree(self.vault), before)

    def test_apply_writes_then_validate_and_restore_roundtrip(self):
        out, code = self.run_cli("sync", apply=True)
        self.assertEqual(code, 0)
        self.assertFalse(out["dry_run"])
        self.assertGreater(out["written"], 0)
        self.assertEqual(out["failures"], 0)
        again, _ = self.run_cli("sync", apply=True)
        self.assertEqual(again["written"], 0)  # idempotent
        val, code = self.run_cli("validate")
        self.assertEqual((code, val["ok"], val["error_count"]), (0, True, 0))
        target = self.tmp / "restored"
        rest, code = self.run_cli("restore", target=str(target))
        self.assertEqual(code, 0)
        self.assertTrue(rest["ok"])
        self.assertTrue((target / "memory.sqlite3").is_file())

    def test_outputs_carry_counts_not_personal_text_or_paths(self):
        for action, extra in (("sync", {"apply": True}), ("validate", {}),
                              ("restore", {"target": str(self.tmp / "r")}), ("status", {})):
            out, _ = self.run_cli(action, **extra)
            text = json.dumps(out)
            self.assertNotIn(SECRET_TEXT, text, action)
            self.assertNotIn(str(self.vault), text, action)
            self.assertNotIn("jr-", text, action)

    def test_secret_like_skip_is_counted_not_shown(self):
        from jarvis.memory import Memory
        Memory(self.state).add("note", "token sk-" + "a" * 30, title="leaky")
        out, code = self.run_cli("sync", apply=True)
        self.assertEqual(out["skipped"].get("secret-like"), 1)
        self.assertFalse(out["complete"])
        self.assertNotIn("sk-", json.dumps(out))

    def test_sync_does_not_need_second_brain_enabled_but_keeps_write_false(self):
        self.assertNotIn("second_brain", self.config)
        _, code = self.run_cli("sync", apply=True)
        self.assertEqual(code, 0)
        self.assertIs(self.config["vault"]["write"], False)


class Guards(Base):
    def test_bulk_sync_cannot_leapfrog_active_capture(self):
        from jarvis import second_brain_queue
        self.assertTrue(second_brain_queue.configure(self.state)["ok"])
        out, code = self.run_cli("sync", apply=True)
        self.assertEqual((code, out["error"]), (1, "capture-active-use-drain"))

    def test_restore_needs_target_and_refuses_overlaps_and_non_empty(self):
        self.run_cli("sync", apply=True)
        full = self.tmp / "full"
        full.mkdir()
        (full / "keep.txt").write_text("mine")
        cases = {None: "restore-needs-target", str(self.state): "restore-target-overlaps-live-data",
                 str(self.state / "sub"): "restore-target-overlaps-live-data",
                 str(self.vault): "restore-target-overlaps-live-data",
                 str(self.vault / "restored"): "restore-target-overlaps-live-data",
                 str(self.tmp): "restore-target-overlaps-live-data",
                 str(full): "restore-target-not-empty"}
        for target, error in cases.items():
            with self.subTest(target=target):
                out, code = self.run_cli("restore", target=target)
                self.assertEqual((code, out["error"]), (1, error))
        self.assertEqual((full / "keep.txt").read_text(), "mine")
        self.assertFalse((self.state / "sub").exists())

    def test_empty_target_is_accepted(self):
        self.run_cli("sync", apply=True)
        target = self.tmp / "empty"
        target.mkdir()
        out, code = self.run_cli("restore", target=str(target))
        self.assertEqual(code, 0)

    def test_unmounted_vault_refuses_writes_and_creates_nothing(self):
        empty = self.tmp / "mount"
        empty.mkdir()
        config = {"vault": {"path": str(empty)}}
        for action in ("sync", "drain"):
            out, code = self.run_cli(action, config=config, apply=True)
            self.assertEqual((code, out["error"]), (1, "vault-sentinel-missing"))
        self.assertEqual(self.tree(empty), [])
        missing = {"vault": {"path": str(self.tmp / "nope")}}
        out, code = self.run_cli("sync", config=missing, apply=True)
        self.assertEqual((code, out["error"]), (1, "vault-unavailable"))
        self.assertFalse((self.tmp / "nope").exists())

    def test_vault_disabled_or_unconfigured_refuses(self):
        for vault, error in (({"enabled": False, "path": str(self.vault)}, "vault-disabled"),
                             ({}, "vault-not-configured"),
                             ({"path": {"windows": r"C:\Users\x\Vault"}}, "vault-not-configured")):
            out, code = self.run_cli("sync", config={"vault": vault}, apply=True)
            self.assertEqual((code, out["error"]), (1, error))

    def test_per_os_path_map(self):
        config = {"vault": {"path": {"windows": r"C:\Users\x\Vault", "linux": str(self.vault)}}}
        with mock.patch("jarvis.vault.on_windows", return_value=False):
            out, code = self.run_cli("sync", config=config)
        self.assertEqual(code, 0)

    def test_validate_reads_an_unmarked_vault_without_writing(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        out, code = self.run_cli("validate", config={"vault": {"path": str(bare)}})
        self.assertEqual((code, out["records"]), (0, 0))
        self.assertEqual(self.tree(bare), [])


class Drain(Base):
    def test_drain_calls_queue_with_bounded_limit_and_returns_numbers(self):
        calls = []

        class Q:
            def drain(self, state, root, limit=100):
                calls.append((str(state), str(root), limit))
                return {"ok": True, "acked": 4, "remaining": 1, "ids": ["SECRET"]}
        out, code = self.run_cli("drain", queue=Q(), limit=10 ** 6)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [(str(self.state), str(self.vault.resolve()), 500)])
        self.assertEqual((out["acked"], out["remaining"]), (4, 1))
        self.assertNotIn("SECRET", json.dumps(out))

    def test_drain_failure_exit_code_and_no_message(self):
        class Q:
            def drain(self, *a, **k):
                raise RuntimeError("body text here")
        out, code = self.run_cli("drain", queue=Q())
        self.assertEqual((code, out["error"]), (1, "RuntimeError"))

    def test_drain_without_queue_module_is_a_clean_refusal(self):
        with mock.patch.dict(sys.modules, {"jarvis.second_brain_queue": None}):
            out, code = self.run_cli("drain")
        self.assertEqual((code, out["error"]), (1, "queue-unavailable"))

    def test_failed_drain_report_exits_nonzero(self):
        class Q:
            def drain(self, *a, **k):
                return {"ok": False, "failed": 3}
        out, code = self.run_cli("drain", queue=Q())
        self.assertEqual((code, out["failed"]), (1, 3))


class StatusCmd(Base):
    def test_status_is_read_only_and_creates_no_stores(self):
        empty = self.tmp / "fresh-state"
        empty.mkdir()
        out, code = rt.cli(ns("status"), self.config, empty)
        self.assertEqual(code, 0)
        self.assertEqual(self.tree(empty), [])
        self.assertEqual(out["state"], "disabled")

    def test_status_does_not_modify_existing_state(self):
        before = {p: (p.stat().st_size, p.stat().st_mtime_ns)
                  for p in self.state.rglob("*") if p.is_file()}
        self.run_cli("status", config={**self.config, "second_brain": {"enabled": True}})
        after = {p: (p.stat().st_size, p.stat().st_mtime_ns)
                 for p in self.state.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class MainWiring(Base):
    def main(self, *argv, config=None):
        cfg = self.tmp / "jarvis.config.json"
        cfg.write_text(json.dumps(config or self.config))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["jarvis", "--config", str(cfg), "--state",
                                             str(self.state), *argv]), \
                redirect_stdout(out), redirect_stderr(err):
            code = entry.main()
        return code, out.getvalue(), err.getvalue()

    def test_overflow_flag_reaches_runtime_without_publishing(self):
        with mock.patch.object(rt, "_cli_overflow", return_value=({"ok": True}, 0)) as call:
            code, out, _ = self.main("archive", "overflow", "--mib", "32")
        self.assertEqual(code, 0)
        self.assertEqual(call.call_args.args[0].mib, 32)
        self.assertEqual(self.tree(self.vault), [".obsidian"])

    def test_sync_defaults_to_dry_run_through_main(self):
        before = self.tree(self.vault)
        code, out, _ = self.main("archive", "sync")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["dry_run"])
        self.assertEqual(self.tree(self.vault), before)

    def test_apply_flag_writes_and_exit_code_reflects_failure(self):
        code, out, _ = self.main("archive", "sync", "--apply")
        self.assertEqual((code, json.loads(out)["dry_run"]), (0, False))
        code, out, _ = self.main("archive", "restore")
        self.assertEqual((code, json.loads(out)["error"]), (1, "restore-needs-target"))

    def test_node_is_refused(self):
        with mock.patch("jarvis.topology.is_node", return_value=True):
            code, out, err = self.main("archive", "sync", "--apply")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("proxy node", err)
        self.assertEqual(self.tree(self.vault), [".obsidian"])

    def test_node_may_still_move_files_to_the_main(self):
        # A node's file commands go through the hub, which only lets it reach the drop folder.
        src = self.tmp / "f.txt"
        src.write_text("hi")
        with mock.patch("jarvis.topology.is_node", return_value=True), \
                mock.patch("jarvis.transfer.send_file", return_value={"sent": "f.txt"}) as send, \
                mock.patch("jarvis.transfer.list_inbox", return_value=["a.txt"]) as inbox:
            code, out, err = self.main("send", str(src))
            self.assertEqual((code, json.loads(out)), (0, {"sent": "f.txt"}), err)
            code, out, err = self.main("inbox")
            self.assertEqual((code, json.loads(out)), (0, ["a.txt"]), err)
        send.assert_called_once()
        inbox.assert_called_once()


if __name__ == "__main__":
    unittest.main()
