"""WS2 main_readiness: local read-only diagnostics, never a promotion authorization.

Filesystem credentials are temp files. No cluster doctor subprocess, no provider call,
and no real credential body is read.
"""

import json
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from jarvis.fleet.readiness import main_readiness

SENTINEL = "CREDENTIAL-SENTINEL-do-not-leak-9f3a"
TOKEN = "0123456789abcdef"
OTHER = "fedcba9876543210"
_WHICH = object()
_BANNED = (
    SENTINEL,
    "auth.json",
    ".credentials.json",
    "local-exec-daemon-credential.json",
)


class MainReadinessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.home.mkdir()
        self.vault.mkdir()
        self.state.mkdir()

    def _config(self, machine="nimbus-7", vault_path=None, vault=True):
        config = {
            "cluster": {"local_device": machine, "main_device": machine},
            "devices": {machine: {"hostname": "irrelevant"}},
        }
        if vault:
            config["vault"] = {"path": str(self.vault if vault_path is None else vault_path)}
        return config

    def _which(self, name):
        return "/usr/bin/" + name

    def _call(self, config=None, state=None, home=_WHICH, environ=_WHICH, which=_WHICH,
              expected=TOKEN, omit_expected=False):
        kwargs = {}
        if home is not _WHICH:
            kwargs["home"] = home
        else:
            kwargs["home"] = self.home
        if environ is not _WHICH:
            kwargs["environ"] = environ
        else:
            kwargs["environ"] = {}
        if which is not _WHICH:
            kwargs["which"] = which
        else:
            kwargs["which"] = self._which
        if not omit_expected:
            kwargs["expected_code"] = expected
        return main_readiness(
            self._config() if config is None else config,
            self.state if state is None else state,
            **kwargs,
        )

    def _item(self, report, name):
        found = [item for item in report["checks"] if item["check"] == name]
        self.assertEqual(len(found), 1, name)
        return found[0]

    def _assert_contract(self, report):
        self.assertEqual(
            set(report), {"ready", "control_ready", "ai_ready", "checks", "limitations"})
        self.assertIs(report["ready"], False)
        self.assertIs(report["ai_ready"], False)
        self.assertIsInstance(report["control_ready"], bool)
        self.assertIsInstance(report["checks"], list)
        self.assertIsInstance(report["limitations"], list)
        self.assertGreaterEqual(len(report["limitations"]), 3)
        limitations = " ".join(report["limitations"]).lower()
        self.assertIn("promot", limitations)
        self.assertIn("no provider call", limitations)
        self.assertIn("unverified", limitations)
        self.assertIn("handoff", limitations)
        blob = json.dumps(report)
        for banned in _BANNED:
            self.assertNotIn(banned, blob)
        self.assertNotIn(str(self.home), blob)
        self.assertNotIn(str(self.vault), blob)
        self.assertNotIn(str(self.state), blob)
        for item in report["checks"]:
            self.assertEqual(set(item) - {"fix"}, {"check", "ok", "detail"})
            self.assertIsInstance(item["ok"], bool)
            self.assertIsInstance(item["detail"], str)
            if item["ok"]:
                self.assertNotIn("fix", item)
            else:
                self.assertIsInstance(item["fix"], str)
                self.assertTrue(item["fix"])
        return blob

    def _write(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def _guard_open(self, forbidden):
        real_open = open
        blocked = {path.resolve() for path in forbidden}

        def guard(file, *args, **kwargs):
            if not isinstance(file, int):
                try:
                    resolved = Path(file).resolve()
                except (OSError, TypeError, ValueError):
                    resolved = None
                if resolved in blocked:
                    raise AssertionError("opened " + Path(file).name)
            return real_open(file, *args, **kwargs)

        return mock.patch("builtins.open", guard)

    def _matched(self):
        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            return self._call(expected=TOKEN)

    def test_control_profile_does_not_authorize_promotion(self):
        self._write(Path(".codex") / "auth.json", SENTINEL + "-codex")
        self._write(Path(".claude") / ".credentials.json", SENTINEL + "-claude")
        self._write(Path(".grokbot") / "local-exec-daemon-credential.json", SENTINEL + "-grok")
        note = self.vault / "UNIQUE-NOTE-NAME-9f3a.md"
        note.write_text(SENTINEL + "-vault")
        database = self.state / "UNIQUE-DB-9f3a.sqlite3"
        database.write_bytes((SENTINEL + "-db").encode())
        before = sorted(path.name for path in self.root.iterdir())
        with self._guard_open((self.home / ".codex" / "auth.json",
                                self.home / ".claude" / ".credentials.json",
                                self.home / ".grokbot" / "local-exec-daemon-credential.json",
                                note, database)), \
                mock.patch("jarvis.fleet.readiness.Path.home", side_effect=AssertionError("home")), \
                mock.patch("subprocess.run", side_effect=AssertionError("run")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("popen")), \
                mock.patch("socket.gethostname", side_effect=AssertionError("host")), \
                mock.patch("socket.getaddrinfo", side_effect=AssertionError("dns")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("connect")), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN) as fingerprint:
            report = self._call()
        fingerprint.assert_called_once()
        self._assert_contract(report)
        self.assertTrue(report["control_ready"])
        self.assertFalse(report["ready"])
        self.assertFalse(report["ai_ready"])
        self.assertTrue(self._item(report, "codex credential presence")["ok"])
        self.assertTrue(self._item(report, "claude credential presence")["ok"])
        self.assertTrue(self._item(report, "grok bot credential presence")["ok"])
        self.assertIn("not authentication", self._item(report, "codex credential presence")["detail"])
        for name in ("vault storage", "state directory"):
            detail = self._item(report, name)["detail"]
            self.assertIn("unverified", detail)
            self.assertIn("handoff", detail)
            self.assertTrue(self._item(report, name)["ok"])
        self.assertNotIn("UNIQUE-NOTE-NAME-9f3a", json.dumps(report))
        self.assertNotIn("UNIQUE-DB-9f3a", json.dumps(report))
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()))

    def test_missing_and_unreadable_credentials_do_not_leak(self):
        missing = self._call(environ={"CODEX_HOME": str(self.root / "missing-codex-home")})
        self._assert_contract(missing)
        codex = self._item(missing, "codex credential presence")
        self.assertFalse(codex["ok"])
        self.assertIn("not present", codex["detail"])
        self.assertNotIn("missing-codex-home", json.dumps(missing))
        self.assertFalse((self.root / "missing-codex-home").exists())

        path = self._write(Path(".codex") / "auth.json", SENTINEL + "-codex")
        path.chmod(0)
        try:
            with self._guard_open((path,)), \
                    mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
                report = self._call()
            self._assert_contract(report)
            self.assertNotIn(str(path), json.dumps(report))
            if not os.access(path, os.R_OK):
                item = self._item(report, "codex credential presence")
                self.assertFalse(item["ok"])
                self.assertIn("not readable", item["detail"])
        finally:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

        real_access = os.access

        def deny_auth(target, mode, *args, **kwargs):
            if Path(target).name == "auth.json":
                return False
            return real_access(target, mode, *args, **kwargs)

        with self._guard_open((path,)), \
                mock.patch("jarvis.fleet.readiness.os.access", side_effect=deny_auth), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            forced = self._call()
        self._assert_contract(forced)
        self.assertFalse(self._item(forced, "codex credential presence")["ok"])
        self.assertIn("not readable", self._item(forced, "codex credential presence")["detail"])
        self.assertTrue(self._item(forced, "vault storage")["ok"])

    def test_environ_argument_is_authoritative(self):
        outside = self.root / "outside-codex"
        outside.mkdir()
        (outside / "auth.json").write_text(SENTINEL + "-outside")
        claude_dir = self.root / "outside-claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text(SENTINEL + "-claude")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(outside), "CLAUDE_CONFIG_DIR": str(claude_dir)}), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            hidden = self._call(environ={})
            shown = self._call(environ={"CODEX_HOME": str(outside), "CLAUDE_CONFIG_DIR": str(claude_dir)})
        self._assert_contract(hidden)
        self._assert_contract(shown)
        self.assertFalse(self._item(hidden, "codex credential presence")["ok"])
        self.assertTrue(self._item(shown, "codex credential presence")["ok"])
        self.assertTrue(self._item(shown, "claude credential presence")["ok"])
        self.assertNotIn(str(outside), json.dumps(shown))
        self.assertNotIn(str(claude_dir), json.dumps(shown))
        self.assertFalse(shown["ready"])
        self.assertFalse(shown["ai_ready"])

    def test_optional_claude_and_grok_presence(self):
        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            absent = self._call()
        self._assert_contract(absent)
        self.assertTrue(self._item(absent, "claude credential presence")["ok"])
        self.assertIn("optional", self._item(absent, "claude credential presence")["detail"])
        self.assertFalse(self._item(absent, "grok bot credential presence")["ok"])
        self.assertFalse(absent["ai_ready"])
        self.assertTrue(absent["control_ready"])

        self._write(Path(".grokbot") / "local-exec-daemon-credential.json", SENTINEL + "-grok")
        with self._guard_open((self.home / ".grokbot" / "local-exec-daemon-credential.json",)), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            present = self._call()
        self._assert_contract(present)
        self.assertTrue(self._item(present, "grok bot credential presence")["ok"])
        self.assertIn("not authentication", self._item(present, "grok bot credential presence")["detail"])
        self.assertFalse(present["ai_ready"])

    def test_unset_and_unreadable_vault(self):
        cases = (
            {},
            {"vault": {}},
            {"vault": {"path": None}},
            {"vault": {"path": ""}},
            {"vault": {"path": "   "}},
            {"vault": {"enabled": False, "path": str(self.vault)}},
            {"cluster": {"local_device": "nimbus-7", "main_device": "nimbus-7"}},
        )
        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            for config in cases:
                report = self._call(config=config)
                self._assert_contract(report)
                self.assertFalse(self._item(report, "vault storage")["ok"])
                self.assertFalse(report["control_ready"])
                self.assertIn("unverified", self._item(report, "vault storage")["detail"])
                self.assertTrue(self._item(report, "tested code")["ok"])

        missing = self.root / "absent-vault"
        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            report = self._call(config=self._config(vault_path=missing))
        self._assert_contract(report)
        self.assertFalse(missing.exists())
        self.assertIn("missing", self._item(report, "vault storage")["detail"])
        self.assertFalse(report["control_ready"])

        if os.name != "nt":
            windows_path = "C:\\not\\configured"
            with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
                report = self._call(config={"vault": {"path": windows_path}})
            self._assert_contract(report)
            self.assertIn("not configured", self._item(report, "vault storage")["detail"])
            self.assertNotIn(windows_path, json.dumps(report))

        key = "windows" if os.name == "nt" else "linux"
        other = "linux" if key == "windows" else "windows"
        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            wrong_os = self._call(config={"vault": {"path": {other: str(self.vault)}}})
            right_os = self._call(config={"vault": {"path": {key: str(self.vault), other: str(missing)}}})
        self._assert_contract(wrong_os)
        self._assert_contract(right_os)
        self.assertFalse(self._item(wrong_os, "vault storage")["ok"])
        self.assertTrue(self._item(right_os, "vault storage")["ok"])

        real_listdir = os.listdir

        def deny_vault(path):
            if os.path.abspath(path) == os.path.abspath(self.vault):
                raise PermissionError(13, "denied")
            return real_listdir(path)

        with mock.patch("jarvis.fleet.readiness.os.listdir", side_effect=deny_vault), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            unreadable = self._call()
        self._assert_contract(unreadable)
        self.assertFalse(self._item(unreadable, "vault storage")["ok"])
        self.assertIn("not readable", self._item(unreadable, "vault storage")["detail"])
        self.assertFalse(unreadable["control_ready"])

    def test_state_directory_missing_is_not_created(self):
        missing = self.root / "new-state"
        file_state = self.root / "state-file"
        file_state.write_text("not-a-directory")
        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            absent = self._call(state=missing)
            filed = self._call(state=file_state)
            blank = self._call(state="  ")
        self.assertFalse(missing.exists())
        for report, needle in ((absent, "missing"), (filed, "not a directory"), (blank, "missing")):
            self._assert_contract(report)
            self.assertFalse(report["control_ready"])
            self.assertIn(needle, self._item(report, "state directory")["detail"])
            self.assertIn("unverified", self._item(report, "state directory")["detail"])
        self.assertNotIn(str(missing), json.dumps(absent))

    def test_code_match_mismatch_missing_and_unavailable(self):
        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN) as fingerprint:
            matched = self._call(expected=TOKEN)
            mismatched = self._call(expected=OTHER)
        self._assert_contract(matched)
        self._assert_contract(mismatched)
        self.assertTrue(self._item(matched, "tested code")["ok"])
        self.assertTrue(matched["control_ready"])
        self.assertFalse(matched["ready"])
        self.assertIn("does not authorize promotion", self._item(matched, "tested code")["detail"])
        self.assertFalse(self._item(mismatched, "tested code")["ok"])
        self.assertFalse(mismatched["control_ready"])
        self.assertEqual(fingerprint.call_count, 2)

        with mock.patch("jarvis.deploy.code_fingerprint") as fingerprint:
            omitted = self._call(omit_expected=True)
            for expected in ("", 123, b"abc"):
                report = self._call(expected=expected)
                self._assert_contract(report)
                self.assertFalse(self._item(report, "tested code")["ok"])
                self.assertIn("not supplied", self._item(report, "tested code")["detail"])
                self.assertFalse(report["control_ready"])
        self._assert_contract(omitted)
        self.assertFalse(self._item(omitted, "tested code")["ok"])
        self.assertIn("not supplied", self._item(omitted, "tested code")["detail"])
        fingerprint.assert_not_called()

        with mock.patch("jarvis.deploy.code_fingerprint", side_effect=OSError("hidden-disk-path")):
            unavailable = self._call(expected=TOKEN)
        self._assert_contract(unavailable)
        self.assertFalse(self._item(unavailable, "tested code")["ok"])
        self.assertIn("unavailable", self._item(unavailable, "tested code")["detail"])
        self.assertNotIn("hidden-disk-path", json.dumps(unavailable))
        self.assertFalse(unavailable["control_ready"])

        for value in (None, ""):
            with mock.patch("jarvis.deploy.code_fingerprint", return_value=value):
                report = self._call(expected=TOKEN)
            self._assert_contract(report)
            self.assertFalse(self._item(report, "tested code")["ok"])
            self.assertFalse(report["control_ready"])

    def test_live_fingerprint_matches_only_the_supplied_value(self):
        from jarvis.deploy import code_fingerprint
        live = None
        matched = None
        for _ in range(3):
            live = code_fingerprint()
            matched = self._call(expected=live)
            if self._item(matched, "tested code")["ok"]:
                break
        self.assertIsInstance(live, str)
        self.assertTrue(live)
        self._assert_contract(matched)
        self.assertTrue(self._item(matched, "tested code")["ok"])
        other = "0" * 16 if live != "0" * 16 else "1" * 16
        mismatched = self._call(expected=other)
        self._assert_contract(mismatched)
        self.assertFalse(self._item(mismatched, "tested code")["ok"])
        self.assertFalse(mismatched["control_ready"])

    def test_arbitrary_machine_ids_do_not_change_control(self):
        machines = (
            "nimbus-7",
            "dell",
            "Zenbook",
            "auto",
            "host with spaces",
            "node/odd",
            "任意id",
            "x" * 80,
        )
        flags = []
        with mock.patch("socket.gethostname", side_effect=AssertionError("host")), \
                mock.patch("socket.getaddrinfo", side_effect=AssertionError("dns")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("connect")), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            for machine in machines:
                config = {
                    "cluster": {"local_device": machine, "main_device": "other"},
                    "devices": {machine: {"hostname": "unused"}, "other": {}},
                    "vault": {"path": str(self.vault)},
                }
                report = self._call(config=config)
                self._assert_contract(report)
                self.assertTrue(self._item(report, "machine id")["ok"])
                self.assertIn("local_device=" + machine, self._item(report, "machine id")["detail"])
                flags.append(report["control_ready"])
                self.assertFalse(report["ready"])
                self.assertFalse(report["ai_ready"])
        self.assertEqual(set(flags), {True})

        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            bare = self._call(config={"vault": {"path": str(self.vault)}})
        self._assert_contract(bare)
        self.assertIn("no machine id configured", self._item(bare, "machine id")["detail"])
        self.assertTrue(bare["control_ready"])

    def test_python_and_sqlite_failures_block_control(self):
        with mock.patch("jarvis.fleet.readiness.sys.version_info", (3, 9, 0)), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            old = self._call()
        self._assert_contract(old)
        self.assertFalse(self._item(old, "python runtime")["ok"])
        self.assertIn("does not satisfy", self._item(old, "python runtime")["detail"])
        self.assertFalse(old["control_ready"])

        with mock.patch("jarvis.fleet.readiness._requires_python", return_value=None), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            unread = self._call()
        self._assert_contract(unread)
        self.assertFalse(self._item(unread, "python runtime")["ok"])
        self.assertFalse(unread["control_ready"])

        with mock.patch("jarvis.fleet.readiness._requires_python", return_value=">=3.10,<4"), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            bounded = self._call()
        self.assertTrue(self._item(bounded, "python runtime")["ok"])

        import sqlite3
        with mock.patch.object(sqlite3, "connect", side_effect=sqlite3.Error("hidden-db-path")), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            broken = self._call()
        self._assert_contract(broken)
        self.assertFalse(self._item(broken, "sqlite")["ok"])
        self.assertNotIn("hidden-db-path", json.dumps(broken))
        self.assertFalse(broken["control_ready"])

        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            healthy = self._call()
        self.assertTrue(self._item(healthy, "python runtime")["ok"])
        self.assertIn("requires-python", self._item(healthy, "python runtime")["detail"])
        self.assertTrue(self._item(healthy, "sqlite")["ok"])
        self.assertTrue(healthy["control_ready"])

    def test_ssh_which_does_not_run_a_connection(self):
        seen = []

        def which(name):
            seen.append(name)
            if name == "ssh-keygen":
                return None
            return "/usr/bin/" + name

        with mock.patch("subprocess.run", side_effect=AssertionError("run")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("popen")), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            report = self._call(which=which)
        self._assert_contract(report)
        self.assertEqual(seen, ["ssh", "ssh-keygen"])
        self.assertFalse(self._item(report, "ssh executables")["ok"])
        self.assertIn("not found", self._item(report, "ssh executables")["detail"])
        self.assertIn("not tested", self._item(report, "ssh executables")["detail"])
        self.assertTrue(report["control_ready"])

        def boom(name):
            if name == "ssh":
                raise OSError("hidden-ssh-path")
            return "/usr/bin/" + name

        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            failed = self._call(which=boom)
        self._assert_contract(failed)
        self.assertFalse(self._item(failed, "ssh executables")["ok"])
        self.assertNotIn("hidden-ssh-path", json.dumps(failed))
        self.assertTrue(failed["control_ready"])

        with mock.patch("jarvis.fleet.readiness.shutil.which", return_value="/usr/bin/ssh") as finder, \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            default = self._call(which=None)
        self.assertEqual([call.args[0] for call in finder.call_args_list], ["ssh", "ssh-keygen"])
        self.assertTrue(self._item(default, "ssh executables")["ok"])
        self.assertNotIn("/usr/bin/ssh", json.dumps(default))

    def test_non_dict_config_fails_closed(self):
        with mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            report = main_readiness(
                None, self.state, home=self.home, environ={}, which=self._which, expected_code=TOKEN)
        self._assert_contract(report)
        self.assertFalse(self._item(report, "vault storage")["ok"])
        self.assertFalse(report["control_ready"])
        self.assertIn("no machine id configured", self._item(report, "machine id")["detail"])

    @contextmanager
    def _as_os(self, system):
        """Point vault_path's OS check at one platform without changing pathlib."""
        platform_name = {"windows": "win32", "linux": "linux", "macos": "darwin"}[system]

        def windows():
            return system == "windows"

        with mock.patch("jarvis.vault.on_windows", windows), \
                mock.patch("jarvis.platforms.on_windows", windows), \
                mock.patch("sys.platform", platform_name):
            yield

    def test_tilde_vault_expands_injected_home_without_leaking(self):
        from jarvis.vault import vault_path
        leaf = self.home / ".ws2-vault"
        leaf.mkdir()
        decoy = self.root / "decoy-home"
        decoy.mkdir()
        mapping = {"windows": "~/.ws2-vault", "linux": "~/.ws2-vault"}
        with mock.patch.dict(os.environ, {"HOME": str(decoy), "USERPROFILE": str(decoy)}), \
                mock.patch("jarvis.fleet.readiness.Path.home", side_effect=AssertionError("home")), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            selected = vault_path({"path": "~/.ws2-vault"})
            plain = self._call(config={"vault": {"path": "~/.ws2-vault"}})
            mapped_selected = vault_path({"path": mapping})
            mapped = self._call(config={"vault": {"path": mapping}})
            missing = self._call(config={"vault": {"path": "~/.ws2-absent"}})
            planted = self.home / "ws2-rel-b"
            planted.mkdir()
            relative = self._call(config={"vault": {"path": "q/ws2-rel-b"}})
        self.assertEqual(selected, "~/.ws2-vault")
        self._assert_contract(plain)
        self._assert_contract(mapped)
        self._assert_contract(missing)
        self._assert_contract(relative)
        self.assertFalse(self._item(relative, "vault storage")["ok"])
        self.assertIn("missing", self._item(relative, "vault storage")["detail"])
        self.assertNotIn(str(planted), json.dumps(relative))
        self.assertFalse(relative["control_ready"])
        self.assertTrue(self._item(plain, "vault storage")["ok"])
        self.assertTrue(plain["control_ready"])
        self.assertFalse(plain["ready"])
        self.assertFalse(plain["ai_ready"])
        if mapped_selected is None:
            self.assertFalse(self._item(mapped, "vault storage")["ok"])
            self.assertIn("not configured", self._item(mapped, "vault storage")["detail"])
            self.assertFalse(mapped["control_ready"])
        else:
            self.assertTrue(self._item(mapped, "vault storage")["ok"])
            self.assertTrue(mapped["control_ready"])
        for report in (plain, mapped, missing):
            blob = json.dumps(report)
            self.assertNotIn(str(leaf), blob)
            self.assertNotIn(str(decoy), blob)
            self.assertNotIn("~/.ws2-vault", blob)
            self.assertNotIn(".ws2-vault", blob)
            self.assertNotIn(".ws2-absent", blob)
        self.assertFalse((decoy / ".ws2-vault").exists())
        self.assertFalse((self.home / ".ws2-absent").exists())
        self.assertFalse((decoy / ".ws2-absent").exists())
        self.assertIn("missing", self._item(missing, "vault storage")["detail"])
        self.assertFalse(missing["control_ready"])
        self.assertTrue(leaf.is_dir())

    def _assert_vault_map(self, system):
        from jarvis.vault import vault_path
        folders = {
            "windows": self.root / "map-windows",
            "linux": self.root / "map-linux",
            "macos": self.root / "map-macos",
        }
        for folder in folders.values():
            folder.mkdir()
        mapping = {key: str(folder) for key, folder in folders.items()}
        drive = "C:\\not\\configured"
        with self._as_os(system), mock.patch("jarvis.deploy.code_fingerprint", return_value=TOKEN):
            selected = vault_path({"path": mapping})
            report = self._call(config={"vault": {"path": dict(mapping)}})
            self._assert_contract(report)
            blob = json.dumps(report)
            for folder in folders.values():
                self.assertNotIn(str(folder), blob)
            item = self._item(report, "vault storage")
            self.assertFalse(report["ready"])
            self.assertFalse(report["ai_ready"])
            if selected is None:
                self.assertFalse(item["ok"])
                self.assertIn("not configured", item["detail"])
                self.assertFalse(report["control_ready"])
            else:
                target = Path(selected)
                self.assertTrue(item["ok"])
                self.assertTrue(report["control_ready"])
                self.assertTrue(target.is_dir())
                hidden = target.with_name(target.name + "-hidden")
                target.rename(hidden)
                try:
                    self.assertTrue(any(folder.is_dir() for folder in folders.values()))
                    again = self._call(config={"vault": {"path": dict(mapping)}})
                finally:
                    hidden.rename(target)
                self._assert_contract(again)
                again_blob = json.dumps(again)
                for folder in folders.values():
                    self.assertNotIn(str(folder), again_blob)
                self.assertNotIn(str(hidden), again_blob)
                self.assertFalse(self._item(again, "vault storage")["ok"])
                self.assertIn("missing", self._item(again, "vault storage")["detail"])
                self.assertFalse(again["control_ready"])
            chosen = vault_path({"path": drive})
            drive_report = self._call(config={"vault": {"path": drive}})
        self._assert_contract(drive_report)
        escaped = json.dumps(drive)[1:-1]
        self.assertNotIn(escaped, json.dumps(drive_report))
        self.assertNotIn(drive.replace("\\", "/"), json.dumps(drive_report))
        drive_item = self._item(drive_report, "vault storage")
        if chosen is None:
            self.assertIn("not configured", drive_item["detail"])
        else:
            self.assertIn("missing", drive_item["detail"])
        self.assertFalse(drive_item["ok"])
        self.assertFalse(Path(drive).exists())

    def test_macos_vault_map_matches_canonical(self):
        self._assert_vault_map("macos")

    def test_windows_vault_map_matches_canonical(self):
        self._assert_vault_map("windows")

    def test_linux_vault_map_matches_canonical(self):
        self._assert_vault_map("linux")
