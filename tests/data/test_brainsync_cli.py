"""Subprocess tests for ``python -m jarvis brain`` from an unrelated directory.

Each test uses a temp config, state, and vault. Peers that sync or export also
have a real git repository. Nothing here reads the checkout's live config,
state, or vault, and nothing opens a live SSH session.

Commands are ``sys.executable -m jarvis`` with ``PYTHONPATH`` set to the
repository and explicit ``--config`` / ``--state`` arguments. Gaps the current
process still has (status creating ``.jarvis``, an unknown action blocking on
stdin, export/freeze truncating a body over 4096 bytes, and the SSH adapter
skipping the peer checkout's ``.venv``) are asserted as the contract.
"""

import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jarvis import brainsync

ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCHEMA = brainsync.EXPORT_SCHEMA
PROMPT_SECONDS = 15


def _child_env():
    """Environment for a CLI child. Drops CODEX metadata and points imports at this repo."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("CODEX_")}
    env["JARVIS_NO_MODELS"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    return env


def _git_env():
    env = _child_env()
    env.update({
        "GIT_AUTHOR_NAME": "jarvis-brainsync",
        "GIT_AUTHOR_EMAIL": "brainsync@localhost",
        "GIT_COMMITTER_NAME": "jarvis-brainsync",
        "GIT_COMMITTER_EMAIL": "brainsync@localhost",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def _git(*args, cwd=None):
    proc = subprocess.run(["git", *args], cwd=cwd, env=_git_env(), capture_output=True, check=False)
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", "replace")[-500:]
        raise AssertionError(f"git {' '.join(args)} failed: {detail}")
    return proc


def _snapshot(root):
    """Paths and hashes under ``root``. ``.git`` objects are omitted; HEAD is kept."""
    root = Path(root)
    if not root.exists():
        return []
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in sorted(dirnames) if name != ".git"]
        filenames.sort()
        current = Path(dirpath)
        rows.append(("dir", current.relative_to(root).as_posix()))
        for name in filenames:
            path = current / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                rows.append(("link", rel, os.readlink(path)))
            elif path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                rows.append(("file", rel, digest))
    if (root / ".git").is_dir():
        rows.append(("head", _git("rev-parse", "HEAD", cwd=root).stdout.decode().strip()))
        rows.append(("porcelain", _git("status", "--porcelain", cwd=root).stdout.decode()))
    return rows


def _pad_over_limit(payload):
    """Valid JSON plus trailing spaces, one byte past the 4096 request cap.

    A silent ``raw[:4096]`` slice still parses. Only a length check rejects it.
    """
    if len(payload) >= 4096:
        raise AssertionError("payload must start under the cap so padding is the only excess")
    padded = payload + b" " * (4097 - len(payload))
    if len(padded) != 4097:
        raise AssertionError(len(padded))
    json.loads(padded.decode("utf-8"))
    json.loads(padded[:4096].decode("utf-8"))
    return padded


class BrainCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.unrelated = self.root / "unrelated cwd"
        self.unrelated.mkdir()
        real = ROOT / "jarvis.config.json"
        self._real_config = real.read_bytes() if real.is_file() else None

    def tearDown(self):
        real = ROOT / "jarvis.config.json"
        if self._real_config is not None and real.is_file():
            self.assertEqual(real.read_bytes(), self._real_config)

    def run_jarvis(self, config, state, action, *extra, input=None, timeout=30):
        argv = [sys.executable, "-m", "jarvis", "--config", str(config), "--state", str(state),
                "brain", action, *extra]
        self.assertTrue(Path(sys.executable).is_absolute(), sys.executable)
        self.assertEqual(argv[0], sys.executable)
        return subprocess.run(argv, cwd=self.unrelated, env=_child_env(), input=input,
                              capture_output=True, timeout=timeout, check=False)

    def write_config(self, base, local, main, vault, *, enabled=False, remote=None, devices=None,
                     filename="jarvis.config.json"):
        peers = devices or {
            "arch": {"hostname": "arch-host", "transport": "local"},
            "dell": {"hostname": "dell-host", "transport": "local"},
        }
        brain = {"enabled": enabled is True}
        if remote is not None:
            brain["remote"] = str(remote)
        config = {
            "cluster": {"enabled": True, "local_device": local, "main_device": main,
                        "main_url": "http://127.0.0.1:7780"},
            "devices": peers,
            "vault": {"enabled": True, "path": str(vault)},
            "brainsync": brain,
        }
        path = Path(base) / filename
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def seed_checkout(self, base):
        """A real commit, separate from the brain repository brainsync creates later."""
        base = Path(base)
        base.mkdir(parents=True, exist_ok=True)
        (base / "README").write_text("peer checkout\n", encoding="utf-8")
        _git("init", "-b", "main", cwd=base)
        _git("add", "README", cwd=base)
        _git("commit", "-m", "seed", cwd=base)
        head = _git("rev-parse", "HEAD", cwd=base).stdout.decode().strip()
        self.assertEqual(len(head), 40)

    def make_peer(self, name, local, main, *, enabled=True, remote=None, files=None, memory=None,
                  devices=None, filename="jarvis.config.json"):
        base = self.root / name
        self.seed_checkout(base)
        vault = base / "vault"
        vault.mkdir()
        for rel, text in (files or {}).items():
            path = vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        state = base / ".jarvis"
        state.mkdir()
        os.chmod(state, 0o700)
        if memory is not None:
            con = sqlite3.connect(state / "memory.sqlite3")
            try:
                con.execute("CREATE TABLE items(body TEXT)")
                for row in memory:
                    con.execute("INSERT INTO items(body) VALUES (?)", (row,))
                con.commit()
            finally:
                con.close()
        config = self.write_config(base, local, main, vault, enabled=enabled, remote=remote,
                                   devices=devices, filename=filename)
        return base, config, vault, state

    def test_status_from_unrelated_cwd_creates_nothing(self):
        base = self.root / "status peer"
        self.seed_checkout(base)
        vault = base / "vault"
        vault.mkdir()
        note = vault / "note.md"
        note.write_text("leave-me", encoding="utf-8")
        config = self.write_config(base, "arch", "arch", vault, enabled=False)
        state = base / ".jarvis"
        before = _snapshot(base)
        proc = self.run_jarvis(config, state, "status")
        after = _snapshot(base)
        self.assertEqual(before, after, "status is read-only and must not create .jarvis\n"
                         f"rc={proc.returncode}\n{proc.stdout.decode()[-400:]}\n"
                         f"{proc.stderr.decode()[-400:]}")
        self.assertFalse(state.exists())
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        body = json.loads(proc.stdout.decode())
        self.assertTrue(body["ok"])
        self.assertTrue(body["readonly"])
        self.assertFalse(body["enabled"])
        self.assertEqual(body["status"], "disabled")
        self.assertEqual(body["device"], "arch")
        self.assertEqual(note.read_text(encoding="utf-8"), "leave-me")
        self.assertFalse((state / "brain-git").exists())

    def test_sync_disabled_refuses_without_changes(self):
        base = self.root / "sync off"
        self.seed_checkout(base)
        vault = base / "vault"
        vault.mkdir()
        note = vault / "note.md"
        note.write_text("leave-me", encoding="utf-8")
        bare = self.root / "unused.git"
        _git("init", "--bare", str(bare))
        config = self.write_config(base, "arch", "arch", vault, enabled=False, remote=bare)
        before = _snapshot(base)
        head = _git("rev-parse", "HEAD", cwd=base).stdout
        proc = self.run_jarvis(config, base / ".jarvis", "sync", timeout=45)
        self.assertEqual(_snapshot(base), before, "disabled sync must not create .jarvis or a brain repo\n"
                         f"rc={proc.returncode}\n{proc.stdout.decode()[-500:]}\n"
                         f"{proc.stderr.decode()[-500:]}")
        self.assertEqual(_git("rev-parse", "HEAD", cwd=base).stdout, head)
        self.assertNotEqual(proc.returncode, 0, proc.stdout.decode())
        body = json.loads(proc.stdout.decode())
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "disabled")
        self.assertFalse(body["enabled"])
        self.assertEqual(note.read_text(encoding="utf-8"), "leave-me")
        self.assertFalse((base / ".jarvis" / "brain-git").exists())

    def test_unknown_action_rejects_while_stdin_stays_open(self):
        base, config, _vault, state = self.make_peer(
            "unknown", "arch", "arch", enabled=False, files={"note.md": "x"})
        argv = [sys.executable, "-m", "jarvis", "--config", str(config), "--state", str(state),
                "brain", "nope"]
        proc = subprocess.Popen(argv, cwd=self.unrelated, env=_child_env(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            try:
                proc.wait(timeout=PROMPT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)
                self.fail("unsupported brain action blocked while stdin was still open; "
                          "it must reject before reading the pipe")
            out = proc.stdout.read()
            err = proc.stderr.read()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe and not pipe.closed:
                    pipe.close()
        self.assertNotEqual(proc.returncode, 0, out.decode())
        self.assertIn(b"Usage: jarvis brain sync|status", err)
        self.assertEqual((base / "vault" / "note.md").read_text(encoding="utf-8"), "x")

    def test_export_rejects_request_over_4096_without_truncating(self):
        files = {"note.md": "export-me"}
        memory = ["cli-memory"]
        small_base, small_config, small_vault, small_state = self.make_peer(
            "export-small", "arch", "arch", files=files, memory=memory)
        body = json.dumps({"schema": EXPORT_SCHEMA, "old_main": "arch", "new_main": "dell"},
                          separators=(",", ":")).encode()
        small = self.run_jarvis(small_config, small_state, "export", input=body, timeout=60)
        self.assertEqual(small.returncode, 0, small.stderr.decode()[-800:])
        decoded = small.stdout.decode("ascii").strip()
        self.assertTrue(decoded)
        self.assertEqual((small_vault / "note.md").read_text(encoding="utf-8"), "export-me")

        base, config, vault, state = self.make_peer(
            "export-over", "arch", "arch", files=files, memory=memory)
        padded = _pad_over_limit(body)
        proc = self.run_jarvis(config, state, "export", input=padded, timeout=60)
        detail = f"rc={proc.returncode} stdout={proc.stdout[:80]!r} stderr={proc.stderr.decode()[-400:]}"
        self.assertNotEqual(proc.returncode, 0, detail)
        self.assertIn("bad_request", proc.stderr.decode(), detail)
        self.assertFalse((state / "brainsync-pending.bin").exists(), detail)
        self.assertFalse((state / "brainsync-pending.json").exists(), detail)
        self.assertFalse((state / "brain-git").exists(), detail)
        self.assertEqual((vault / "note.md").read_text(encoding="utf-8"), "export-me")

    def test_freeze_rejects_request_over_4096_without_truncating(self):
        base, config, vault, state = self.make_peer(
            "freeze-over", "arch", "arch", files={"note.md": "stay"})
        spec = {"schema": EXPORT_SCHEMA, "old_main": "arch", "new_main": "dell",
                "bundle_sha256": "ab" * 32}
        padded = _pad_over_limit(json.dumps(spec, separators=(",", ":")).encode())
        proc = self.run_jarvis(config, state, "freeze", input=padded, timeout=30)
        detail = proc.stdout.decode()[-500:] + proc.stderr.decode()[-400:]
        self.assertNotEqual(proc.returncode, 0, detail)
        body = json.loads(proc.stdout.decode() or "null")
        self.assertEqual(body.get("error"), "bad_request", detail)
        self.assertFalse((state / "brainsync-handoff.json").exists(), detail)
        self.assertFalse((state / "brain-git").exists(), detail)
        self.assertEqual((vault / "note.md").read_text(encoding="utf-8"), "stay")

    def test_sync_and_receive_round_trip_from_unrelated_cwd(self):
        bare = self.root / "remote.git"
        _git("init", "--bare", str(bare))
        arch, arch_config, arch_vault, arch_state = self.make_peer(
            "arch", "arch", "arch", remote=bare, files={"arch.md": "from-arch"}, memory=["source-row"])
        dell, dell_config, dell_vault, dell_state = self.make_peer(
            "dell", "dell", "dell", remote=bare, files={"dell.md": "from-dell"})
        first = self.run_jarvis(arch_config, arch_state, "sync", timeout=60)
        second = self.run_jarvis(dell_config, dell_state, "sync", timeout=60)
        third = self.run_jarvis(arch_config, arch_state, "sync", timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr.decode()[-800:])
        self.assertEqual(second.returncode, 0, second.stderr.decode()[-800:])
        self.assertEqual(third.returncode, 0, third.stderr.decode()[-800:])
        self.assertTrue(json.loads(first.stdout.decode())["ok"])
        self.assertEqual((dell_vault / "arch.md").read_text(encoding="utf-8"), "from-arch")
        self.assertEqual((arch_vault / "dell.md").read_text(encoding="utf-8"), "from-dell")
        self.assertEqual((arch_vault / "arch.md").read_text(encoding="utf-8"), "from-arch")
        self.assertEqual((dell_vault / "dell.md").read_text(encoding="utf-8"), "from-dell")

        request = json.dumps({"schema": EXPORT_SCHEMA, "old_main": "arch", "new_main": "dell"},
                             separators=(",", ":")).encode()
        exported = self.run_jarvis(arch_config, arch_state, "export", input=request, timeout=60)
        self.assertEqual(exported.returncode, 0, exported.stderr.decode()[-800:])
        import base64
        blob = base64.b64decode("".join(exported.stdout.decode("ascii").split()), validate=True)
        self.assertTrue(blob.startswith(brainsync._MAGIC))
        received = self.run_jarvis(dell_config, dell_state, "receive", input=blob, timeout=60)
        self.assertEqual(received.returncode, 0, received.stderr.decode()[-800:])
        report = json.loads(received.stdout.decode())
        self.assertTrue(report["ok"], report)
        con = sqlite3.connect(dell_state / "memory.sqlite3")
        try:
            rows = [row[0] for row in con.execute("SELECT body FROM items")]
        finally:
            con.close()
        self.assertEqual(rows, ["source-row"])
        self.assertEqual((arch_vault / "arch.md").read_text(encoding="utf-8"), "from-arch")

    def test_spaced_config_paths_survive_argv(self):
        base = self.root / "Program Files" / "JARVIS peer"
        self.seed_checkout(base)
        vault = base / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("spaced", encoding="utf-8")
        config = self.write_config(base, "arch", "arch", vault, enabled=False, filename="jarvis config.json")
        state = base / ".jarvis"
        self.assertIn(" ", str(config))
        self.assertIn(" ", str(state))
        self.assertIn("Program Files", str(config))
        help_proc = self.run_jarvis(config, state, "-h")
        self.assertEqual(help_proc.returncode, 0, help_proc.stderr.decode())
        help_text = help_proc.stdout.decode()
        self.assertNotIn("export", help_text)
        self.assertNotIn("receive", help_text)
        self.assertNotIn("freeze", help_text)
        proc = self.run_jarvis(config, state, "status")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-500:])
        body = json.loads(proc.stdout.decode())
        self.assertTrue(body["ok"])
        self.assertEqual(body["device"], "arch")
        self.assertFalse(body["enabled"])
        self.assertEqual((vault / "note.md").read_text(encoding="utf-8"), "spaced")

    def test_ssh_adapter_runs_peer_venv_and_config_from_unrelated_cwd(self):
        """The production adapter must select the peer checkout, not login ``python``.

        ``deploy._remote`` is not replaced. Its ``ssh`` exec is the only stand-in:
        the remote command string is run with bash from an unrelated directory.
        A decoy ``python`` on ``PATH`` refuses to import the real checkout.
        """
        peer, peer_config, peer_vault, peer_state = self.make_peer(
            "peer checkout", "dell", "dell", files={"moved.md": "from-peer"}, memory=["peer-row"])
        venv_bin = peer / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        peer_python = venv_bin / "python"
        peer_marker = self.root / "peer-python-marker"
        login_marker = self.root / "login-python-marker"
        peer_python.write_text(_peer_python_script(peer, peer_marker), encoding="utf-8")
        peer_python.chmod(0o755)
        login_bin = self.root / "login-bin"
        login_bin.mkdir()
        for name in ("python", "python3"):
            script = login_bin / name
            script.write_text(_login_python_script(login_marker), encoding="utf-8")
            script.chmod(0o755)
        arch = self.root / "arch-caller"
        arch.mkdir()
        caller_state = arch / ".jarvis"
        devices = {
            "arch": {"hostname": "arch-host", "transport": "local"},
            "dell": {"hostname": "dell-host", "transport": "ssh", "target": "dell@127.0.0.1",
                     "jarvis_dir": str(peer)},
        }
        config = {
            "cluster": {"enabled": True, "local_device": "arch", "main_device": "arch",
                        "main_url": "http://127.0.0.1:7780"},
            "devices": devices,
            "vault": {"enabled": True, "path": str(peer_vault)},
            "brainsync": {"enabled": True},
        }
        seen = []
        notes = []

        def local_ssh(argv, **kwargs):
            command = argv[-1] if isinstance(argv, (list, tuple)) and argv else ""
            program = Path(str(argv[0])).name if isinstance(argv, (list, tuple)) and argv else ""
            if program != "ssh" or not isinstance(command, str):
                return original(argv, **kwargs)
            seen.append(command)
            safe = command in brainsync._FIXED_COMMANDS or str(self.root) in command
            if not safe or "\n" in command or "\x00" in command:
                return subprocess.CompletedProcess(argv, 97, b"", b"refused unexpected remote command\n")
            child = {
                "PATH": f"{login_bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
                "HOME": str(self.root / "fake-home"),
                "LANG": "C",
                "LC_ALL": "C",
                "JARVIS_NO_MODELS": "1",
            }
            (self.root / "fake-home").mkdir(exist_ok=True)
            # ssh runs one remote command string. A plain argv runs as a list so the
            # test hook does not treat a shell as a launcher. Shell syntax still goes
            # through bash, which is what a peer login shell would do.
            try:
                if any(token in command for token in ("|", ";", "&", "$", "`", ">", "<")):
                    bash = shutil.which("bash")
                    if not bash:
                        return subprocess.CompletedProcess(argv, 97, b"", b"bash missing\n")
                    launched = [bash, "-c", command]
                else:
                    launched = shlex.split(command)
                proc = original(launched, cwd=self.unrelated, env=child,
                                input=kwargs.get("input"), capture_output=True,
                                timeout=30, check=False)
            except Exception as exc:
                proc = subprocess.CompletedProcess(argv, 97, b"", f"{type(exc).__name__}: {exc}".encode())
            notes.append(f"rc={proc.returncode} stderr={proc.stderr!r} stdout={proc.stdout[:60]!r}")
            return proc

        import jarvis.deploy as deploy_mod
        # Bind the real runner before the assignment below. deploy.subprocess is the
        # stdlib module, so replacing its run attribute replaces every caller's too.
        original = deploy_mod.subprocess.run
        deploy_mod.subprocess.run = local_ssh
        try:
            request = json.dumps({"schema": EXPORT_SCHEMA, "old_main": "dell", "new_main": "arch"},
                                 separators=(",", ":")).encode()
            result = brainsync.SshBundleTransport(config, arch).pull("dell", request)
        finally:
            deploy_mod.subprocess.run = original

        command = seen[-1] if seen else ""
        tokens = shlex.split(command) if command else []
        detail = (f"command={command!r} tokens={tokens!r} result={result!r} notes={notes!r} "
                  f"peer_marker={peer_marker.read_text() if peer_marker.exists() else None!r} "
                  f"login_marker={login_marker.read_text() if login_marker.exists() else None!r}")
        self.assertTrue(seen, detail)
        self.assertIn(str(peer_python), command, detail)
        self.assertIn("--config", tokens, detail)
        self.assertIn(str(peer_config), tokens, detail)
        self.assertIn("--state", tokens, detail)
        self.assertIn(str(peer_state), tokens, detail)
        self.assertIn(str(peer), command, detail)
        self.assertTrue(peer_marker.is_file(), detail)
        self.assertFalse(login_marker.exists(), detail)
        self.assertTrue(result.get("ok"), detail)
        self.assertEqual((peer_vault / "moved.md").read_text(encoding="utf-8"), "from-peer")
        self.assertFalse(caller_state.exists() and (caller_state / "brain-git").exists())


def _sh_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def _peer_python_script(peer, marker):
    real = Path(sys.executable).resolve()
    return "\n".join([
        "#!/bin/sh",
        "set -u",
        f"marker={_sh_quote(marker)}",
        f"peer={_sh_quote(peer)}",
        'printf "%s\\n" "$PWD" "$@" > "$marker"',
        'config=""',
        'state=""',
        'prev=""',
        'for arg in "$@"; do',
        '  if [ "$prev" = "--config" ]; then config=$arg; fi',
        '  if [ "$prev" = "--state" ]; then state=$arg; fi',
        "  prev=$arg",
        "done",
        'case "$config" in',
        '  "$peer"/*) ;;',
        '  *) echo "peer venv refused unbound config" >&2; exit 87 ;;',
        "esac",
        'case "$state" in',
        '  "$peer"/*) ;;',
        '  *) echo "peer venv refused unbound state" >&2; exit 87 ;;',
        "esac",
        f"export PYTHONPATH={_sh_quote(ROOT)}",
        "export JARVIS_NO_MODELS=1",
        f"exec {_sh_quote(real)} \"$@\"",
        "",
    ])


def _login_python_script(marker):
    return "\n".join([
        "#!/bin/sh",
        f"marker={_sh_quote(marker)}",
        'printf "%s\\n" "$PWD" "$@" > "$marker"',
        'echo "login python is not the peer checkout venv" >&2',
        "exit 86",
        "",
    ])


if __name__ == "__main__":
    unittest.main()
