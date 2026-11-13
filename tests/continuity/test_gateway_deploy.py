import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from jarvis.continuity import gateway_deploy as gd

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
TS = "20261113T120000Z"


class FakeRunner:
    def __init__(self, ssh_code=0, scp_code=0):
        self.calls = []
        self.ssh_code = ssh_code
        self.scp_code = scp_code

    def __call__(self, cmd, input_text=None, timeout=300):
        self.calls.append((list(cmd), input_text))
        head = cmd[0]
        if head == "git":
            out = "abc1234\n" if "rev-parse" in cmd else " M jarvis/x.py\n"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        code = {"ssh": self.ssh_code, "scp": self.scp_code}[head]
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    def of(self, name):
        return [c for c in self.calls if c[0][0] == name]


class DeployBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.lines = []
        self.runner = FakeRunner()

    def run_deploy(self, **kw):
        kw.setdefault("runner", self.runner)
        kw.setdefault("out", self.lines.append)
        kw.setdefault("work_dir", self.work)
        kw.setdefault("now", NOW)
        kw.setdefault("key", "/k/key")
        return gd.deploy(**kw)

    def tar_names(self):
        tgz = next(self.work.glob("jarvis-gateway-*.tgz"))
        with tarfile.open(tgz) as tar:
            return tgz, tar.getnames()


class PackTests(DeployBase):
    def test_dry_run_tarball_contents(self):
        self.assertEqual(self.run_deploy(dry_run=True), 0)
        _, names = self.tar_names()
        self.assertIn("jarvis/continuity/gateway.py", names)
        self.assertIn("jarvis/web/phone/index.html", names)
        self.assertNotIn("jarvis/web/app.js", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])
        self.assertFalse([n for n in names if ".jarvis" in n.split("/")])
        self.assertFalse([n for n in names if ".pre-" in n])
        self.assertTrue(all(n == "jarvis" or n.startswith("jarvis/") for n in names))

    def test_manifest_and_sha(self):
        self.run_deploy(dry_run=True)
        manifest = json.loads((self.work / "manifest.json").read_text())
        tgz, _ = self.tar_names()
        import hashlib
        self.assertEqual(manifest["sha256"], hashlib.sha256(tgz.read_bytes()).hexdigest())
        self.assertEqual(manifest["git_head"], "abc1234")
        self.assertTrue(manifest["dirty"])

    def test_tarball_is_reproducible(self):
        a, sha_a, _ = gd.build_tarball(self.work)
        with tempfile.TemporaryDirectory() as other:
            _, sha_b, _ = gd.build_tarball(other)
        self.assertEqual(sha_a.__len__(), 64)
        self.assertEqual(sha_a, sha_b)


class DryRunTests(DeployBase):
    def test_dry_run_runs_nothing_remote(self):
        self.run_deploy(dry_run=True)
        self.assertEqual(self.runner.of("ssh"), [])
        self.assertEqual(self.runner.of("scp"), [])
        text = "\n".join(self.lines)
        self.assertIn("would run: scp", text)
        self.assertIn("bash -s", text)
        self.assertIn("systemctl restart jarvis-continuity-gateway.service", text)

    def test_script_steps_without_restart(self):
        self.run_deploy(dry_run=True)
        script = "\n".join(self.lines)
        for needle in (
            "set -euo pipefail",
            "sudo -n true",
            'sudo -n cp -a "$OPT" "$OPT.pre-$TS"',
            'sudo -n cp -a "$CONF" "$CONF.pre-$TS"',
            "TS=" + TS,
            "STAGE=/opt/jarvis.new",
            "-u jarvis-gateway env PYTHONPATH=\"$STAGE\" JARVIS_NO_MODELS=1 /usr/bin/python3 "
            '-c "import jarvis.continuity.gateway"',
            'sudo -n rm -rf "$OPT" && sudo -n mv "$STAGE" "$OPT"',
            "chown -R jarvis-gateway:jarvis-gateway",
            'chmod 0750 "$OPT"',
        ):
            self.assertIn(needle, script)
        self.assertNotIn("restart_and_wait; then", script)
        self.assertIn("service NOT restarted", script)
        # import check comes before swap, backup before staging
        self.assertLess(script.index("cp -a"), script.index("tar -xzf"))
        self.assertLess(script.index("import jarvis.continuity.gateway"), script.index("mv \"$STAGE\""))

    def test_script_is_valid_bash(self):
        for script in (
            gd.build_deploy_script("a" * 64, TS, restart=True, patch=True),
            gd.build_deploy_script("a" * 64, TS, restart=False),
            gd.build_rollback_script(restart=True),
        ):
            res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
            self.assertEqual(res.returncode, 0, res.stderr)


class RestartTests(DeployBase):
    def test_restart_path_has_auto_rollback(self):
        self.run_deploy(dry_run=True, restart=True)
        script = "\n".join(self.lines)
        self.assertIn("if restart_and_wait; then", script)
        self.assertIn('restore "$OPT.pre-$TS" "$CONF.pre-$TS"', script)
        self.assertIn("exit 3", script)
        self.assertIn("is-active", script)
        self.assertIn("seq 1 15", script)

    def test_real_run_uses_scp_then_single_ssh(self):
        code = self.run_deploy(restart=True)
        self.assertEqual(code, 0)
        scp, = self.runner.of("scp")
        ssh, = self.runner.of("ssh")
        self.assertEqual(scp[0][:2], ["scp", "-i"])
        self.assertTrue(scp[0][-1].startswith("ubuntu@203.0.113.77:/tmp/jarvis-gateway-"))
        self.assertEqual(ssh[0], ["ssh", "-i", "/k/key", "-o", "BatchMode=yes", "-o",
                                  "ConnectTimeout=10", gd.DEFAULT_HOST, "bash", "-s"])
        self.assertIn("restart_and_wait", ssh[1])
        self.assertTrue(self.lines[-1].endswith("done"))

    def test_no_restart_prints_human_command(self):
        self.assertEqual(self.run_deploy(), 0)
        text = "\n".join(self.lines)
        self.assertIn("NOT restarted", text)
        self.assertIn("sudo -n systemctl restart jarvis-continuity-gateway.service", text)
        self.assertNotIn("restart_and_wait; then", self.runner.of("ssh")[0][1])

    def test_remote_failure_exit_code_propagates(self):
        self.runner.ssh_code = 3
        self.assertEqual(self.run_deploy(restart=True), 3)
        self.assertIn("rollback attempted", "\n".join(self.lines))

    def test_scp_failure_skips_ssh(self):
        self.runner.scp_code = 1
        self.assertEqual(self.run_deploy(), 1)
        self.assertEqual(self.runner.of("ssh"), [])


class RollbackTests(DeployBase):
    def test_rollback_script(self):
        code = self.run_deploy(rollback=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.runner.of("scp"), [])
        script = self.runner.of("ssh")[0][1]
        self.assertIn("ls -d \"$OPT\".pre-*", script)
        self.assertIn("gateway.json.pre-*", script)
        self.assertIn('restore "$OPT_BAK" "$CONF_BAK"', script)
        self.assertIn("service NOT restarted", script)
        self.assertNotIn("restart_and_wait ||", script)
        self.assertFalse(list(self.work.glob("*.tgz")))

    def test_rollback_with_restart(self):
        self.run_deploy(rollback=True, restart=True)
        self.assertIn("restart_and_wait ||", self.runner.of("ssh")[0][1])

    def test_rollback_dry_run(self):
        self.assertEqual(self.run_deploy(rollback=True, dry_run=True), 0)
        self.assertEqual(self.runner.calls, [])

    def test_rollback_rejects_patch(self):
        with self.assertRaises(gd.DeployError):
            self.run_deploy(rollback=True, config_patch="x.json")


class ConfigPatchTests(DeployBase):
    def patch_file(self, text):
        p = self.work / "patch.json"
        p.write_text(text)
        return str(p)

    def test_patch_uploaded_merged_and_validated(self):
        self.run_deploy(config_patch=self.patch_file('{"lease_seconds": 20}'))
        self.assertEqual(len(self.runner.of("scp")), 2)
        script = self.runner.of("ssh")[0][1]
        self.assertIn("load_gateway_config('$MERGED')", script)
        self.assertIn("chmod 0600", script)
        self.assertIn("cfg.update(upd)", script)
        # validated against staged code, before the swap
        self.assertLess(script.index("load_gateway_config"), script.index('mv "$STAGE"'))
        self.assertLess(script.index('mv "$STAGE"'), script.index('mv "$MERGED" "$CONF"'))

    def test_bad_patch_rejected_locally(self):
        with self.assertRaises(gd.DeployError):
            self.run_deploy(config_patch=self.patch_file("[1]"))
        with self.assertRaises(gd.DeployError):
            self.run_deploy(config_patch=self.patch_file("{nope"))
        self.assertEqual(self.runner.of("ssh"), [])

    def test_merge_snippet_semantics(self):
        base = self.work / "base.json"
        patch = self.work / "p.json"
        out = self.work / "out.json"
        base.write_text('{"a": 1, "b": 2}')
        patch.write_text('{"b": 3, "c": 4}')
        res = subprocess.run(["python3", "-c", gd._MERGE_PY, str(base), str(patch), str(out)],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(out.read_text()), {"a": 1, "b": 3, "c": 4})
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)


class CliTests(DeployBase):
    def test_main_dry_run_no_remote(self):
        buf = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(buf):
            code = gd.main(["--dry-run", "--out-dir", str(self.work)])
        self.assertEqual(code, 0)
        self.assertIn("[dry-run] remote script:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
