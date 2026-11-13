"""Terminal sandbox audit 2026-11-13: hides can't be unmounted, every repo's hooks/config are
read-only, the microphone is masked. Real-sandbox tests skip where bwrap can't run."""
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import terminal

CONFIG = {"terminal": {"sandbox": True}}
EPERM, ENOSYS, ALLOW = 0x00050001, 0x00050000 | 38, 0x7FFF0000
X86, A64 = 0xC000003E, 0xC00000B7


def run_bpf(blob, arch, nr, arg0=0):
    """Verdict of a classic-BPF seccomp program for one syscall (a tiny interpreter)."""
    insns = [struct.unpack("HBBI", blob[i:i + 8]) for i in range(0, len(blob), 8)]
    data = {0: nr, 4: arch, 16: arg0 & 0xFFFFFFFF}
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = insns[pc]
        pc += 1
        if code == 0x20:
            acc = data.get(k, 0)
        elif code == 0x06:
            return k
        elif code == 0x15:
            pc += jt if acc == k else jf
        elif code == 0x35:
            pc += jt if acc >= k else jf
        elif code == 0x45:
            pc += jt if acc & k else jf
        else:
            raise AssertionError(f"unexpected BPF opcode {code:#x}")


class SeccompProgramTests(unittest.TestCase):
    def test_x86_64_denies_mounts_and_namespaces(self):
        blob = terminal.seccomp_program("x86_64")
        self.assertEqual(len(blob) % 8, 0)
        for name, nr in {"mount": 165, "umount2": 166, "pivot_root": 155, "chroot": 161, "unshare": 272,
                         "setns": 308, "open_tree": 428, "move_mount": 429, "fsopen": 430,
                         "fsconfig": 431, "fsmount": 432, "fspick": 433, "mount_setattr": 442}.items():
            self.assertEqual(run_bpf(blob, X86, nr), EPERM, name)

    def test_aarch64_denies_mounts_and_namespaces(self):
        blob = terminal.seccomp_program("aarch64")
        for name, nr in {"mount": 40, "umount2": 39, "pivot_root": 41, "unshare": 97, "setns": 268,
                         "move_mount": 429, "mount_setattr": 442}.items():
            self.assertEqual(run_bpf(blob, A64, nr), EPERM, name)
        self.assertEqual(run_bpf(blob, A64, 220, 0x10000000), EPERM)  # clone(CLONE_NEWUSER)
        self.assertEqual(run_bpf(blob, A64, 63), ALLOW)  # read

    def test_clone_with_namespace_flags_is_denied_but_plain_clone_works(self):
        blob = terminal.seccomp_program("x86_64")
        for flag in (0x10000000, 0x00020000, 0x40000000, 0x20000000, 0x08000000, 0x04000000, 0x02000000, 0x80):
            self.assertEqual(run_bpf(blob, X86, 56, flag | 17), EPERM, hex(flag))
        self.assertEqual(run_bpf(blob, X86, 56, 17), ALLOW)  # fork(): SIGCHLD
        self.assertEqual(run_bpf(blob, X86, 56, 0x003D0F00), ALLOW)  # a thread: CLONE_VM|FS|FILES|SIGHAND|...
        self.assertEqual(run_bpf(blob, X86, 435), ENOSYS)  # clone3: libc falls back to clone

    def test_ordinary_calls_pass_and_foreign_abis_fail(self):
        blob = terminal.seccomp_program("x86_64")
        for nr in (0, 1, 2, 59, 257):  # read write open execve openat
            self.assertEqual(run_bpf(blob, X86, nr), ALLOW)
        self.assertEqual(run_bpf(blob, X86, 165 | 0x40000000), EPERM)  # x32 mount
        self.assertEqual(run_bpf(blob, 0x40000003, 21), EPERM)  # 32-bit x86 mount via int 0x80
        self.assertEqual(run_bpf(blob, A64, 63), EPERM)  # the wrong table is never trusted

    def test_unknown_architecture_refuses(self):
        with self.assertRaises(OSError):
            terminal.seccomp_program("riscv64")


class SandboxArgvTests(unittest.TestCase):
    def build(self):
        with mock.patch.object(terminal, "SOCKET_DIRS", ()):
            return terminal.sandbox("/tmp", config=CONFIG)

    def test_argv_drops_capabilities_and_loads_the_filter_from_stdin(self):
        args = self.build()
        if not args:
            self.skipTest("bwrap not available here")
        i = args.index("--cap-drop")
        self.assertEqual(args[i + 1], "ALL")
        j = args.index("--seccomp")
        self.assertEqual(args[j + 1], "0")
        self.assertLess(j, len(args) - 1)  # before the final "--"

    def test_stdin_carries_the_filter_for_a_sandboxed_command_only(self):
        fd, release = terminal._sandbox_stdin(["bwrap", "--seccomp", "0", "--", "true"])
        try:
            self.assertEqual(os.read(fd, 4096), terminal.seccomp_program())
        finally:
            release()
        fd, release = terminal._sandbox_stdin(["bash", "-c", "true"])
        self.assertEqual(fd, subprocess.DEVNULL)
        release()

    def test_no_filter_means_no_run(self):
        args = self.build()
        if not args:
            self.skipTest("bwrap not available here")
        with mock.patch.object(terminal, "seccomp_program", side_effect=OSError("no filter for this CPU")):
            with self.assertRaises(OSError):
                terminal._run_shell([*args, "true"], "/tmp")
            with mock.patch.object(terminal, "sandbox", return_value=args), \
                    mock.patch.object(terminal, "cwd", return_value="/tmp"):
                ok, text = terminal.run("echo should-not-run")
        self.assertFalse(ok)
        self.assertNotIn("should-not-run", text)
        self.assertIn("Couldn't start a shell", text)

    def test_sandbox_off_by_config_still_runs_unfiltered(self):
        self.assertEqual(terminal.sandbox("/tmp", config={"terminal": {"sandbox": False}}), [])
        fd, release = terminal._sandbox_stdin(["bash", "-c", "true"])
        self.assertEqual(fd, subprocess.DEVNULL)
        release()


def sandbox_works():
    if not shutil.which("bwrap"):
        return False
    with mock.patch.object(terminal, "SOCKET_DIRS", ()):
        args = terminal.sandbox("/tmp", config=CONFIG)
    if not args:
        return False
    try:
        return terminal._run_shell([*args, "true"], "/tmp")[0] == 0
    except OSError:
        return False


@unittest.skipUnless(sandbox_works(), "bubblewrap can't run a sandbox here")
class RealSandboxTests(unittest.TestCase):
    def shell(self, script, folder="/tmp", writable=None):
        patches = [mock.patch.object(terminal, "SOCKET_DIRS", ())]
        if writable is not None:
            patches.append(mock.patch.object(terminal, "WRITABLE", tuple(str(w) for w in writable)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        args = terminal.sandbox(folder, config=CONFIG)
        code, out, err, timed_out = terminal._run_shell([*args, "bash", "-c", script], folder)
        self.assertFalse(timed_out)
        return code, out, err

    def test_umount_of_a_hidden_folder_fails_and_it_stays_hidden(self):
        ssh = Path.home() / ".ssh"
        if not ssh.is_dir():
            self.skipTest("no ~/.ssh to hide")
        for name in (".ssh", ):
            code, out, err = self.shell(f"ls -A ~/{name} | wc -l; umount ~/{name}; echo rc=$?; "
                                        f"umount -l ~/{name}; echo lazy_rc=$?; ls -A ~/{name} | wc -l")
        lines = out.split()
        self.assertEqual(lines[0], "0")
        self.assertNotEqual(lines[1], "rc=0")
        self.assertNotEqual(lines[2], "lazy_rc=0")
        self.assertEqual(lines[3], "0", "the real ~/.ssh came back after umount")

    def test_mount_and_namespace_calls_fail_with_eperm(self):
        code, out, err = self.shell(
            "mount -t tmpfs none /mnt 2>&1; echo mount=$?; unshare -m true 2>&1; echo unshare=$?; "
            "unshare -Ur true 2>&1; echo userns=$?; mount --bind /tmp /mnt 2>&1; echo bind=$?; "
            "grep CapEff /proc/self/status")
        for key in ("mount", "unshare", "userns", "bind"):
            self.assertRegex(out, rf"{key}=[1-9]", out)
        self.assertIn("CapEff:\t0000000000000000", out)

    def test_python_cannot_mount_directly(self):
        script = ("python3 - <<'PY'\nimport ctypes, os\nlibc = ctypes.CDLL(None, use_errno=True)\n"
                  "print('mount', libc.mount(b'none', b'/mnt', b'tmpfs', 0, None), ctypes.get_errno())\n"
                  "print('umount2', libc.umount2(os.path.expanduser('~/.ssh').encode(), 2), ctypes.get_errno())\n"
                  "print('unshare', libc.unshare(0x10000000), ctypes.get_errno())\nPY")
        code, out, err = self.shell(script)
        if "mount" not in out:
            self.skipTest("no python3 in the sandbox")
        for line in out.splitlines():
            parts = line.split()
            self.assertEqual(parts[1], "-1", line)
            self.assertEqual(parts[2], "1", line)  # EPERM

    def test_git_hooks_and_config_are_read_only_at_any_depth_but_git_works(self):
        if not shutil.which("git"):
            self.skipTest("no git")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "a" / "b" / "deep"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            code, out, err = self.shell(
                "touch .git/hooks/post-checkout 2>&1; echo hook=$?; echo x >> .git/config 2>&1; echo cfg=$?; "
                "git config core.hooksPath /tmp/evil 2>&1; echo gitcfg=$?; echo hi > f && git add f && "
                "git -c user.name=t -c user.email=t@t commit -qm one 2>&1; echo commit=$?; git status --short; "
                "git log --oneline | wc -l", folder=str(repo), writable=[root])
            self.assertRegex(out, r"hook=[1-9]", out)
            self.assertRegex(out, r"cfg=[1-9]", out)
            self.assertRegex(out, r"gitcfg=[1-9]", out)
            self.assertIn("commit=0", out)
            self.assertFalse((repo / ".git" / "hooks" / "post-checkout").exists())
            self.assertNotIn("evil", (repo / ".git" / "config").read_text())

    def test_the_microphone_node_reads_as_dev_null(self):
        nodes = sorted(Path("/dev/snd").glob("pcmC*D*c"))
        if not nodes:
            self.skipTest("no ALSA capture nodes on this machine")
        code, out, err = self.shell(f"stat -L -c '%t:%T' {nodes[0]}")
        self.assertEqual(out.strip(), "1:3")  # /dev/null


class GitGuardDiscoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def fake_gitdir(self, path, hooks=True):
        (path / "hooks" if hooks else path).mkdir(parents=True, exist_ok=True)
        (path / "config").write_text("[core]\n")
        (path / "HEAD").write_text("ref: refs/heads/main\n")
        return path

    def test_finds_repos_at_depth_submodules_gitfiles_and_worktrees(self):
        deep = self.root / "x" / "y" / "z"
        self.fake_gitdir(deep / ".git")
        sub = self.fake_gitdir(deep / ".git" / "modules" / "lib")
        (deep / "lib").mkdir()
        (deep / "lib" / ".git").write_text("gitdir: ../.git/modules/lib\n")
        wt = self.root / "wt"
        wt.mkdir()
        wtdir = deep / ".git" / "worktrees" / "wt"
        wtdir.mkdir(parents=True)
        (wtdir / "commondir").write_text("../..\n")
        (wt / ".git").write_text(f"gitdir: {wtdir}\n")
        guards = set(terminal._git_guards([self.root]))
        for expected in (deep / ".git" / "hooks", deep / ".git" / "config",
                         sub / "hooks", sub / "config", wtdir.parent.parent / "hooks"):
            self.assertIn(str(expected), guards)

    def test_skips_dependency_trees_symlinks_and_gitdirs_outside_the_roots(self):
        self.fake_gitdir(self.root / "node_modules" / "pkg" / ".git")
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        self.fake_gitdir(outside / "real" / ".git")
        (self.root / "link").symlink_to(outside)
        (self.root / "g").mkdir()
        (self.root / "g" / ".git").write_text(f"gitdir: {outside / 'real' / '.git'}\n")
        self.assertEqual(terminal._git_guards([self.root]), [])

    def test_the_walk_is_bounded(self):
        for i in range(30):
            (self.root / f"d{i}").mkdir()
        self.fake_gitdir(self.root / "d29" / ".git")
        self.assertNotEqual(terminal._git_guards([self.root], budget=1000), [])
        self.assertEqual(terminal._git_guards([self.root], budget=1), [])  # budget spent before it got there
        deep = self.root
        for i in range(8):
            deep = deep / f"n{i}"
        self.fake_gitdir(deep / ".git")
        self.assertNotIn(str(deep / ".git" / "hooks"), terminal._git_guards([self.root], depth=3))

    def test_the_sandbox_binds_each_found_guard_read_only(self):
        self.fake_gitdir(self.root / "r" / "s" / ".git")
        with mock.patch.object(terminal, "WRITABLE", (str(self.root),)), \
                mock.patch.object(terminal, "SOCKET_DIRS", ()):
            args = terminal.sandbox("/tmp", config=CONFIG)
        if not args:
            self.skipTest("bwrap not available here")
        hooks = str(self.root / "r" / "s" / ".git" / "hooks")
        self.assertIn(hooks, args)
        i = args.index(hooks)
        self.assertEqual(args[i - 1:i + 2], ["--ro-bind", hooks, hooks])
        self.assertGreater(i, args.index(str(self.root)))  # after the writable bind, so it wins


if __name__ == "__main__":
    unittest.main()
