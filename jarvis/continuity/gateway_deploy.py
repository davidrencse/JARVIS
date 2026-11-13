"""Deploy the local `jarvis/` package to the Oracle continuity gateway.

    .venv/bin/python -m jarvis.continuity.gateway_deploy [--dry-run] [--restart]
        [--host ubuntu@HOST] [--key KEY] [--config-patch FILE] [--rollback]

Flow: pack `jarvis/` into a tar.gz, `scp` it to /tmp, then run ONE
`ssh ... bash -s` whose script (on stdin) backs up /opt/jarvis and the gateway
config, stages the new tree in /opt/jarvis.new, import-checks it as the service
user, swaps it in and (only with --restart) restarts the unit, rolling back on
failure. `--rollback` restores the newest `.pre-*` backups.

Every subprocess call goes through one injectable `runner`, so tests never
touch ssh. `--dry-run` builds the tarball and prints the script and commands.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HOST = "ubuntu@203.0.113.77"
DEFAULT_KEY = "~/.ssh/oracle-vm.key"
UNIT = "jarvis-continuity-gateway.service"
OPT = "/opt/jarvis"
STAGE = "/opt/jarvis.new"
CONF = "/var/lib/jarvis-gateway/gateway.json"
SERVICE_USER = "jarvis-gateway"
PYTHON = "/usr/bin/python3"
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
REPO_ROOT = Path(__file__).resolve().parents[2]

_PHONE = ("jarvis", "web", "phone")


class DeployError(Exception):
    pass


def default_runner(cmd, input_text=None, timeout=300):
    """Run one command; return an object with returncode/stdout/stderr."""
    return subprocess.run(
        cmd, input=input_text, capture_output=True, text=True, timeout=timeout, check=False)


# --------------------------------------------------------------------------- pack

def _excluded(parts, name, is_dir):
    if "__pycache__" in parts or ".jarvis" in parts:
        return True
    if not is_dir and (name.endswith((".pyc", ".pyo")) or ".pre-" in name):
        return True
    if parts[:2] == ("jarvis", "web"):
        # web/ is the console; only web/phone/ ships to the gateway.
        if len(parts) == 2:
            return False
        return parts[:3] != _PHONE
    return False


def build_tarball(dest_dir, root=REPO_ROOT):
    """Pack `<root>/jarvis` into dest_dir. Return (path, sha256 hex, file count)."""
    pkg = Path(root) / "jarvis"
    if not pkg.is_dir():
        raise DeployError(f"no jarvis/ package under {root}")
    buf = io.BytesIO()
    count = 0
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for dirpath, dirnames, filenames in os.walk(pkg):
                rel_dir = Path(dirpath).relative_to(root)
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not _excluded(rel_dir.parts + (d,), d, True))
                if not _excluded(rel_dir.parts, rel_dir.name, True):
                    info = tar.gettarinfo(dirpath, arcname=rel_dir.as_posix())
                    _normalise(info)
                    tar.addfile(info)
                for name in sorted(filenames):
                    path = Path(dirpath) / name
                    parts = rel_dir.parts + (name,)
                    if _excluded(parts, name, False) or path.is_symlink() or not path.is_file():
                        continue
                    info = tar.gettarinfo(str(path), arcname="/".join(parts))
                    _normalise(info)
                    with open(path, "rb") as fh:
                        tar.addfile(info, fh)
                    count += 1
    data = buf.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    out = Path(dest_dir) / f"jarvis-gateway-{sha[:12]}.tgz"
    out.write_bytes(data)
    return out, sha, count


def _normalise(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def git_info(runner, root=REPO_ROOT):
    """Short HEAD sha and dirty flag, or (None, None) when git is unavailable."""
    try:
        head = runner(["git", "-C", str(root), "rev-parse", "--short", "HEAD"])
        if head.returncode != 0:
            return None, None
        dirty = runner(["git", "-C", str(root), "status", "--porcelain", "--", "jarvis"])
        return head.stdout.strip() or None, (bool(dirty.stdout.strip()) if dirty.returncode == 0 else None)
    except (OSError, subprocess.SubprocessError):
        return None, None


# ------------------------------------------------------------------------ scripts

_MERGE_PY = """\
import json, os, sys
base, patch, out = sys.argv[1:4]
with open(base, encoding="utf-8") as fh:
    cfg = json.load(fh)
with open(patch, encoding="utf-8") as fh:
    upd = json.load(fh)
if not isinstance(cfg, dict) or not isinstance(upd, dict):
    raise SystemExit("config and patch must be JSON objects")
cfg.update(upd)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
if hasattr(os, "O_CLOEXEC"):
    flags |= os.O_CLOEXEC
fd = os.open(out, flags, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2, sort_keys=True)
    fh.write("\\n")
"""

_FUNCS = f"""\
UNIT={UNIT}
OPT={OPT}
CONF={CONF}

restore() {{ # $1 = /opt backup dir, $2 = config backup file
  sudo -n rm -rf "$OPT"
  sudo -n cp -a "$1" "$OPT"
  sudo -n cp -a "$2" "$CONF"
}}

restart_and_wait() {{
  sudo -n systemctl restart "$UNIT" || return 1
  for _ in $(seq 1 15); do
    systemctl is-active --quiet "$UNIT" || return 1
    sleep 1
  done
  systemctl is-active --quiet "$UNIT" || return 1
  return 0
}}
"""


def build_deploy_script(sha, ts, *, restart, patch=False):
    """Bash for the remote side of a deploy. `sha` names the uploaded tarball."""
    tgz = f"/tmp/jarvis-gateway-{sha[:12]}.tgz"
    patch_path = f"/tmp/jarvis-gateway-patch-{sha[:12]}.json"
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "cd /",
        f"TS={ts}",
        f"TGZ={tgz}",
        f"PATCH={patch_path}",
        f"STAGE={STAGE}",
        f"MERGED={CONF}.merging",
        _FUNCS,
        'cleanup() { sudo -n rm -rf "$STAGE" "$MERGED" "$TGZ" "$PATCH" 2>/dev/null || true; }',
        "trap cleanup EXIT",
        "",
        'echo "[remote] sudo check"',
        "sudo -n true",
        "",
        'echo "[remote] backup -> $OPT.pre-$TS, $CONF.pre-$TS"',
        'sudo -n cp -a "$OPT" "$OPT.pre-$TS"',
        'sudo -n cp -a "$CONF" "$CONF.pre-$TS"',
        "",
        'echo "[remote] stage $STAGE"',
        'sudo -n rm -rf "$STAGE"',
        'sudo -n mkdir "$STAGE"',
        'sudo -n tar -xzf "$TGZ" -C "$STAGE" --no-same-owner',
        f'sudo -n chown -R {SERVICE_USER}:{SERVICE_USER} "$STAGE"',
        'sudo -n chmod 0750 "$STAGE"',
        'sudo -n chmod -R o-rwx "$STAGE"',
        "",
        'echo "[remote] import check (as service user, staged tree)"',
        f"sudo -n -u {SERVICE_USER} env PYTHONPATH=\"$STAGE\" JARVIS_NO_MODELS=1 {PYTHON} "
        '-c "import jarvis.continuity.gateway"',
    ]
    if patch:
        lines += [
            "",
            'echo "[remote] merge config patch + validate (load_gateway_config)"',
            "MERGE_PY=$(cat <<'PY'",
            _MERGE_PY.rstrip("\n"),
            "PY",
            ")",
            'sudo -n rm -f "$MERGED"',
            f'sudo -n {PYTHON} -c "$MERGE_PY" "$CONF" "$PATCH" "$MERGED"',
            f'sudo -n chown {SERVICE_USER}:{SERVICE_USER} "$MERGED"',
            'sudo -n chmod 0600 "$MERGED"',
            f"sudo -n -u {SERVICE_USER} env PYTHONPATH=\"$STAGE\" JARVIS_NO_MODELS=1 {PYTHON} "
            "-c \"from jarvis.continuity.gateway import load_gateway_config; "
            "load_gateway_config('$MERGED')\"",
        ]
    lines += [
        "",
        'echo "[remote] swap"',
        'sudo -n mv "$OPT" "$OPT.outgoing-$TS"',
        'if ! sudo -n mv "$STAGE" "$OPT"; then',
        '  echo "[remote] swap FAILED; restoring live tree" >&2',
        '  sudo -n mv "$OPT.outgoing-$TS" "$OPT" || echo "[remote] restore of live tree FAILED" >&2',
        '  exit 1',
        'fi',
        'sudo -n rm -rf "$OPT.outgoing-$TS" || true',
        f'sudo -n chown -R {SERVICE_USER}:{SERVICE_USER} "$OPT"',
        'sudo -n chmod 0750 "$OPT"',
    ]
    if patch:
        lines += [
            'echo "[remote] install merged config"',
            'sudo -n mv "$MERGED" "$CONF"',
            f'sudo -n chown {SERVICE_USER}:{SERVICE_USER} "$CONF"',
            'sudo -n chmod 0600 "$CONF"',
        ]
    if restart:
        lines += [
            "",
            'echo "[remote] restart $UNIT"',
            "if restart_and_wait; then",
            '  echo "[remote] active"',
            "else",
            '  echo "[remote] restart FAILED; rolling back" >&2',
            '  sudo -n journalctl -u "$UNIT" -n 20 --no-pager >&2 || true',
            '  restore "$OPT.pre-$TS" "$CONF.pre-$TS"',
            '  restart_and_wait || echo "[remote] rollback restart FAILED too" >&2',
            "  exit 3",
            "fi",
        ]
    else:
        lines += ["", 'echo "[remote] deployed; service NOT restarted"']
    return "\n".join(lines) + "\n"


def build_rollback_script(*, restart):
    """Bash that restores the newest /opt/jarvis.pre-* and gateway.json.pre-*."""
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "cd /",
        _FUNCS,
        'echo "[remote] sudo check"',
        "sudo -n true",
        'OPT_BAK=$(ls -d "$OPT".pre-* 2>/dev/null | sort | tail -n 1 || true)',
        'CONF_BAK=$(sudo -n bash -c \'ls -d /var/lib/jarvis-gateway/gateway.json.pre-* 2>/dev/null\' '
        "| sort | tail -n 1 || true)",
        '[ -n "$OPT_BAK" ] || { echo "no $OPT.pre-* backup" >&2; exit 4; }',
        '[ -n "$CONF_BAK" ] || { echo "no $CONF.pre-* backup" >&2; exit 4; }',
        'echo "[remote] restoring $OPT_BAK and $CONF_BAK"',
        'restore "$OPT_BAK" "$CONF_BAK"',
    ]
    if restart:
        lines += [
            'echo "[remote] restart $UNIT"',
            'restart_and_wait || { echo "[remote] restart FAILED after rollback" >&2; exit 3; }',
            'echo "[remote] active"',
        ]
    else:
        lines += ['echo "[remote] restored; service NOT restarted"']
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- run

def _ssh_base(host, key):
    return ["ssh", "-i", key, *SSH_OPTS, host]


def restart_command(host, key):
    return " ".join(shlex.quote(p) for p in
                    [*_ssh_base(host, key), "sudo", "-n", "systemctl", "restart", UNIT])


def _show(result, out):
    for stream in (result.stdout, result.stderr):
        for line in (stream or "").splitlines():
            out(line)


def deploy(host=DEFAULT_HOST, key=DEFAULT_KEY, *, dry_run=False, restart=False,
           config_patch=None, rollback=False, runner=default_runner, out=print,
           now=None, work_dir=None, root=REPO_ROOT):
    """Run a deploy (or rollback). Return a process exit code."""
    key = os.path.expanduser(key)
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    ssh = _ssh_base(host, key)

    if rollback:
        if config_patch:
            raise DeployError("--rollback cannot be combined with --config-patch")
        script = build_rollback_script(restart=restart)
        if dry_run:
            out("[dry-run] remote script:")
            out(script)
            out("[dry-run] would run: " + " ".join(shlex.quote(p) for p in [*ssh, "bash", "-s"]))
            return 0
        out(f"[deploy] rollback on {host}")
        res = runner([*ssh, "bash", "-s"], input_text=script)
        _show(res, out)
        if res.returncode:
            out(f"[deploy] rollback FAILED (exit {res.returncode})")
            return res.returncode
        if not restart:
            out("[deploy] restored, not restarted. Restart with: " + restart_command(host, key))
        return 0

    patch_file = None
    if config_patch:
        patch_file = Path(config_patch)
        try:
            data = json.loads(patch_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DeployError(f"bad --config-patch file: {exc}") from None
        if not isinstance(data, dict):
            raise DeployError("--config-patch must hold a JSON object")

    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="jarvis-gw-deploy-"))
    work.mkdir(parents=True, exist_ok=True)
    tarball, sha, count = build_tarball(work, root=root)
    head, dirty = git_info(runner, root)
    manifest = {"sha256": sha, "files": count, "git_head": head, "dirty": dirty,
                "built_utc": ts, "tarball": tarball.name}
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    out(f"[deploy] packed {count} files {tarball.name} sha256={sha[:12]} "
        f"git={head or '?'}{' dirty' if dirty else ''}")

    remote_tgz = f"/tmp/jarvis-gateway-{sha[:12]}.tgz"
    remote_patch = f"/tmp/jarvis-gateway-patch-{sha[:12]}.json"
    scp = ["scp", "-i", key, *SSH_OPTS]
    scp_cmds = [[*scp, str(tarball), f"{host}:{remote_tgz}"]]
    if patch_file:
        scp_cmds.append([*scp, str(patch_file), f"{host}:{remote_patch}"])
    script = build_deploy_script(sha, ts, restart=restart, patch=bool(patch_file))

    if dry_run:
        out("[dry-run] remote script:")
        out(script)
        for cmd in scp_cmds:
            out("[dry-run] would run: " + " ".join(shlex.quote(p) for p in cmd))
        out("[dry-run] would run: " + " ".join(shlex.quote(p) for p in [*ssh, "bash", "-s"])
            + " < script")
        if not restart:
            out("[dry-run] no restart; afterwards run: " + restart_command(host, key))
        return 0

    for cmd in scp_cmds:
        out(f"[deploy] upload {Path(cmd[-2]).name}")
        res = runner(cmd)
        if res.returncode:
            _show(res, out)
            out(f"[deploy] upload FAILED (exit {res.returncode}); nothing changed on {host}")
            return res.returncode
    out(f"[deploy] run remote script on {host}")
    res = runner([*ssh, "bash", "-s"], input_text=script)
    _show(res, out)
    if res.returncode:
        out(f"[deploy] FAILED (exit {res.returncode})"
            + ("; rollback attempted" if res.returncode == 3 else ""))
        return res.returncode
    out("[deploy] done" + ("" if restart else "; service NOT restarted"))
    if not restart:
        out("[deploy] restart with: " + restart_command(host, key))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m jarvis.continuity.gateway_deploy")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument("--dry-run", action="store_true", help="build tarball, print script, run nothing remote")
    parser.add_argument("--restart", action="store_true", help="restart the unit; auto-rollback on failure")
    parser.add_argument("--config-patch", metavar="FILE", help="JSON file merged (top level) into gateway.json")
    parser.add_argument("--rollback", action="store_true", help="restore newest /opt/jarvis.pre-* + config backup")
    parser.add_argument("--out-dir", help="keep the tarball and manifest here (default: temp dir)")
    args = parser.parse_args(argv)
    try:
        return deploy(args.host, args.key, dry_run=args.dry_run, restart=args.restart,
                      config_patch=args.config_patch, rollback=args.rollback,
                      work_dir=args.out_dir)
    except DeployError as exc:
        print(f"[deploy] error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
