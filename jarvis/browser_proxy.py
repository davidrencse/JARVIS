"""The signed-in browsers for a Claude brain, behind the same checks as ChatGPT's.

ChatGPT's tools lane reaches the JARVIS browsers (actions.browser_mcp: Playwright MCP and
agent-browser) through Codex, which asks JARVIS before every MCP tool call: approve_browser
(only the JARVIS browsers, no blocked tools, only public http(s) pages), then browser_gate
(credentials, card numbers and 2FA codes refused; commit-like clicks wait for David while
rules.confirm asks). The Claude CLI has no such per-call question, so a Claude brain
(inference.claude_brain) reaches each browser through this relay instead:

    python -m jarvis --config ... --state ... browser-proxy playwright

It starts the real server from browser_mcp's spec and passes JSON-RPC through both ways,
except: tools/list answers leave out the disabled tools, and every tools/call goes through
the same approve_browser + browser_gate first. A refused call never reaches the browser;
Claude gets an error result saying why.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

# The CLI hands MCP servers a minimal environment; Chrome and npx need these.
PASS_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "HYPRLAND_INSTANCE_SIGNATURE",
            "DBUS_SESSION_BUS_ADDRESS", "HOME", "PATH", "LOCALAPPDATA", "APPDATA",
            "USERPROFILE", "PROGRAMFILES", "SYSTEMROOT", "TEMP", "TMP")


def servers(config, state):
    """browser_mcp's specs for the configured engines; {} when the browser is off."""
    from .actions import BROWSER_ENGINES, browser_mcp
    actions = (config or {}).get("actions", {})
    if not actions.get("enabled", True) or not actions.get("browser", True):
        return {}
    return browser_mcp(state, actions.get("browser_engines", BROWSER_ENGINES))


def mcp_entries(config, config_path, state, python=None):
    """--mcp-config entries that start one relay per browser server."""
    python = python or sys.executable
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    return {name: {"command": python,
                   "args": ["-m", "jarvis", "--config", str(config_path), "--state", str(state),
                            "browser-proxy", name],
                   "env": env}
            for name in servers(config, state)}


def approver(config, state):
    """approve(params) -> (ok, why): exactly what the Codex lane uses."""
    from .actions import approve_browser
    from .browser_gate import gate
    return gate(approve_browser, config, state)


def params_for(server, tool, args):
    """The approval params Codex would send for this call (the shape the gates read)."""
    return {"serverName": server, "message": f'Allow the {server} MCP server to run tool "{tool}"?',
            "_meta": {"tool_params": args if isinstance(args, dict) else {}}}


class Relay:
    def __init__(self, name, spec, approve, stdin, stdout):
        self.name, self.spec, self.approve = name, spec, approve
        self.stdin, self.stdout = stdin, stdout
        self.hidden = set(spec.get("disabled_tools") or ())
        self.lists, self.lock = set(), threading.Lock()  # ids of tools/list requests in flight
        self.calls = {}  # id -> tool name of tools/call requests in flight

    def _write(self, message):
        line = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        with self.lock:
            self.stdout.write(line.rstrip("\n") + "\n")
            self.stdout.flush()

    def _spawn(self):
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (self.spec.get("env") or {}).items()})
        return subprocess.Popen([self.spec["command"], *self.spec.get("args", [])],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, env=env)

    def _from_server(self, proc):
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue  # only JSON-RPC may reach the client's stdout
            try:
                self._filter(message)
                self._write(message)
            except Exception:  # one malformed reply never ends the relay
                try:
                    self._write(message)
                except Exception:
                    continue

    def _filter(self, message):
        if not isinstance(message, dict) or "result" not in message:
            return
        with self.lock:
            listed = self._listed(message.get("id"))
            try:
                tool = self.calls.pop(message.get("id"), "")
            except TypeError:
                tool = ""
        result = message["result"]
        if not listed:
            from .browser_gate import observe_result
            # Snapshots name refs for David's questions; only a snapshot-bearing tool's reply
            # counts, never page text a read tool returned.
            observe_result(self.name, result, tool)
        tools = result.get("tools") if isinstance(result, dict) else None
        if listed and isinstance(tools, list):
            result["tools"] = [t for t in tools if not isinstance(t, dict)
                               or t.get("name") not in self.hidden]

    def _listed(self, rid):
        """True (and forgets it) if `rid` was a tools/list request; unhashable ids aren't."""
        try:
            if rid in self.lists:
                self.lists.discard(rid)
                return True
        except TypeError:
            pass
        return False

    def _refuse(self, rid, why):
        self._write({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": f"Refused: {why}"}], "isError": True}})

    def _error(self, rid, message):
        self._write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": message}})

    def run(self, proc=None):
        proc = proc or self._spawn()
        reader = threading.Thread(target=self._from_server, args=(proc,), daemon=True)
        reader.start()
        try:
            for line in self.stdin:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    # A JSON-RPC batch (or junk) would skip the per-call gate: never forwarded.
                    for item in message if isinstance(message, list) else ():
                        if isinstance(item, dict) and "id" in item:
                            self._error(item["id"], "batch requests are not supported")
                    continue
                if message.get("method") == "tools/list" and "id" in message:
                    try:
                        with self.lock:
                            self.lists.add(message["id"])
                    except TypeError:
                        pass  # unhashable id: the reply just isn't filtered by id
                if message.get("method") == "tools/call":
                    params = message.get("params")
                    params = params if isinstance(params, dict) else {}
                    tool, args = str(params.get("name") or ""), params.get("arguments") or {}
                    try:
                        ok, why = (False, f"{tool} is not allowed") if tool in self.hidden else \
                            (False, "the tool arguments are not an object") if not isinstance(args, dict) else \
                            self.approve(params_for(self.name, tool, args))
                    except Exception as exc:  # a broken check refuses, never waves through
                        ok, why = False, f"the browser check failed ({type(exc).__name__})"
                    if ok:
                        from .browser_gate import observe_call
                        observe_call(self.name, tool, args)
                        try:
                            with self.lock:
                                self.calls[message.get("id")] = tool
                        except TypeError:
                            pass
                    if not ok:
                        if "id" in message:
                            self._refuse(message["id"], why)
                        continue
                proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            reader.join(timeout=2)
            if not reader.is_alive() and proc.stdout:
                proc.stdout.close()
        return 0


def serve(config, base, state, name, stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for stream in (stdin, stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n" if stream is stdout else None)
    spec = servers(config, Path(state)).get(name)
    if spec is None:
        print(f"No JARVIS browser server named {name!r}.", file=sys.stderr)
        return 2
    return Relay(name, spec, approver(config, Path(state)), stdin, stdout).run()
