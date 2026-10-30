"""Local coding agent launchers with saved prompts and bounded process lifetimes."""

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from .platforms import NO_WINDOW
from .fastlane import subscription_env
from .prompts import build_prompt, normalize_task


def workspace(spec, config, base):
    value = spec.get("workspace", ".")
    if not isinstance(value, str):
        raise ValueError("workspace must be a path string.")
    root = (base / value).resolve()
    allowed = [(base / p).resolve() for p in config.get("workspace_roots", [])]
    if not root.is_dir() or not any(root == p or p in root.parents for p in allowed):
        raise ValueError("Workspace must exist under a configured workspace_roots entry.")
    return root


def make_plan(spec, config, base):
    normalize_task(spec)
    root = workspace(spec, config, base)
    provider = spec.get("provider", "codex")
    mode = spec.get("mode", "read-only")
    if provider not in ("codex", "claude"):
        raise ValueError("provider must be codex or claude.")
    if mode not in ("read-only", "workspace-write"):
        raise ValueError("mode must be read-only or workspace-write.")
    if spec.get("role") in ("reviewer", "researcher", "assistant") and mode != "read-only":
        raise ValueError("Reviewers and researchers require read-only mode.")
    power = spec.get("power", "mid")
    if power not in POWERS:
        raise ValueError("power must be low, mid, or high.")
    tier = config.get("models", {}).get(provider, {}).get(power, {})
    binary = shutil.which(provider)
    if not binary:
        raise ValueError(f"{provider} is not installed or is not on PATH.")
    if Path(binary).suffix.lower() in (".cmd", ".bat"):
        raise ValueError(f"Install the native {provider} executable; shell shims are not supported.")
    if provider == "codex":
        args = [binary, "exec", "--json", "--color", "never", "--sandbox", mode,
                "-c", 'forced_login_method="chatgpt"',  # never API-key (per-token) billing
                "--skip-git-repo-check", "-C", str(root)]
        if tier.get("model"):
            args += ["-m", tier["model"]]
        if tier.get("effort"):
            args += ["-c", f'model_reasoning_effort="{tier["effort"]}"']
        args.append("-")
    else:
        args = [binary, "--print", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "plan" if mode == "read-only" else "acceptEdits",
                "--permission-prompts", "none"]
        if tier.get("model"):
            args += ["--model", tier["model"]]
        if tier.get("effort"):
            args += ["--effort", tier["effort"]]
        if spec.get("role") == "assistant":
            from .fastlane import LEAN_FLAGS
            args += LEAN_FLAGS  # quick answers: skip plugin MCP servers, skills, shell
    boundary = ("Read-only execution: inspect and propose; do not modify files or run mutating commands."
                if mode == "read-only" else
                "Workspace-write execution: implement the scoped task under the runtime's permissions.")
    return {"provider": provider, "workspace": str(root), "mode": mode, "power": power,
            "model": tier.get("model", "default"), "effort": tier.get("effort", "default"),
            "argv": args, "prompt": boundary + "\n\n" + build_prompt({**spec, "provider": provider})}


POWERS = ("low", "mid", "high")


def final_message(provider, directory):
    """The agent's last answer from a finished run's saved output, or None."""
    directory = Path(directory)
    if provider == "codex":
        last = directory / "last-message.md"
        if last.is_file():  # a blank file falls through to the streamed answer
            text = last.read_text(encoding="utf-8").strip()
            if text:
                return text
    result = None
    stdout = directory / "stdout.jsonl"
    if not stdout.is_file():
        return None
    with stdout.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                result = event["result"]
            item = event.get("item")
            if (event.get("type") == "item.completed" and isinstance(item, dict)
                    and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
                result = item["text"]
    return (result or "").strip() or None


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=15, shell=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def run_task(store, task_id, config, base, timeout=900):
    from .topology import is_node
    if is_node(config):
        raise ValueError("Model tasks run on the main PC. Submit this request through jarvis ask.")
    if not 1 <= timeout <= 3600:
        raise ValueError("timeout must be between 1 and 3600 seconds.")
    task = store.get(task_id)
    plan = make_plan(task["spec"], config, base)
    store.transition(task_id, "queued", "running",
                     {"provider": plan["provider"], "mode": plan["mode"]})
    directory = store.directory / "runs" / task_id
    process = None
    state = "failed"
    detail = {}
    try:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "prompt.md").write_text(plan["prompt"], encoding="utf-8")
        if plan["provider"] == "codex":
            plan["argv"][-1:-1] = ["-o", str(directory / "last-message.md")]
        with (directory / "stdout.jsonl").open("wb") as stdout, \
                (directory / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(
                plan["argv"], cwd=plan["workspace"], stdin=subprocess.PIPE,
                stdout=stdout, stderr=stderr, shell=False,
                start_new_session=os.name != "nt", env=subscription_env(),
                creationflags=NO_WINDOW)
            try:
                process.communicate(plan["prompt"].encode("utf-8"), timeout=timeout)
                state = "finished" if process.returncode == 0 else "failed"
                detail = {"exit_code": process.returncode,
                          "note": "Process exit is not acceptance verification."}
            except subprocess.TimeoutExpired:
                stop_process(process)
                state = "timed_out"
            except KeyboardInterrupt:
                stop_process(process)
                state = "interrupted"
    except BaseException as exc:
        if process is not None:
            stop_process(process)
        detail = {"error_type": type(exc).__name__}
        raise
    finally:
        store.transition(task_id, "running", state, {**detail, "artifacts": str(directory)})
    from .usage import record_run
    record_run(directory, plan["provider"], plan.get("model"))
    return {"task_id": task_id, "state": state, "artifacts": str(directory),
            "answer": final_message(plan["provider"], directory), **detail}
