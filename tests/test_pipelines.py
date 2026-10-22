"""Per-pipeline contract and regression tests.

Each class covers one pipeline end to end at its own boundary (ledger, planning,
brain lane, console HTTP, voice, credits, probes). The bugs fixed in the 2026-10-21
audit each have a test here that fails on the old code.
"""

import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from jarvis import probe
from jarvis.chatgpt import ChatGPTLane
from jarvis.credits import Credits
from jarvis.desktop import lan_mouse_config
from jarvis.memory import Memory
from jarvis.orchestrator import Orchestrator, validate_plan
from jarvis.router import specialists
from jarvis.store import Store
from jarvis.ui import Console, Handler

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "jarvis.config.json").read_text(encoding="utf-8"))
CATALOG = specialists(ROOT)
FINGERPRINT = ":".join(["ab"] * 32)


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()


def dead_pid():
    """The pid of a process that has definitely exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------- task ledger pipeline
class LedgerRecoveryTests(Temp):
    def claim(self, store, pid):
        task = store.add({"objective": "x"})
        store.transition(task["id"], "queued", "running")
        with store.connect() as db:  # pretend a different process made the claim
            db.execute("UPDATE events SET detail=? WHERE task_id=? AND kind='running'",
                       (json.dumps({"pid": pid}), task["id"]))
        return task["id"]

    def test_dead_owner_does_not_lock_the_ledger_forever(self):
        store = Store(self.base)
        stuck = self.claim(store, dead_pid())
        nxt = store.add({"objective": "y"})["id"]
        store.transition(nxt, "queued", "running")  # used to raise "Another task is running"
        self.assertEqual(store.get(stuck)["state"], "failed")
        self.assertEqual(store.get(nxt)["state"], "running")
        last = store.events(stuck)[-1]
        self.assertEqual(json.loads(last["detail"])["error_type"], "OwnerDied")

    def test_live_owner_keeps_its_claim(self):
        store = Store(self.base)
        live = self.claim(store, __import__("os").getpid())
        other = store.add({"objective": "y"})["id"]
        with self.assertRaises(ValueError):
            store.transition(other, "queued", "running")
        self.assertEqual(store.get(live)["state"], "running")
        self.assertEqual(store.get(other)["state"], "queued")

    def test_claims_without_a_pid_expire_only_when_old(self):
        store = Store(self.base)
        task = store.add({"objective": "x"})["id"]
        with store.connect() as db:
            db.execute("UPDATE tasks SET state='running' WHERE id=?", (task,))
        self.assertEqual(store.reap_stale(), [])  # fresh: could be a legitimate old-format run
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with store.connect() as db:
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old, task))
        self.assertEqual(store.reap_stale(), [task])

    def test_running_claim_records_its_owner(self):
        store = Store(self.base)
        task = store.add({"objective": "x"})["id"]
        store.transition(task, "queued", "running", {"provider": "claude"})
        detail = json.loads(store.events(task)[-1]["detail"])
        self.assertEqual(detail["provider"], "claude")
        self.assertIsInstance(detail["pid"], int)


# ---------------------------------------------------------------- planning pipeline
class PlanHardeningTests(Temp):
    def test_specialist_files_with_windows_line_endings_still_load(self):
        agents = self.base / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "scout.md").write_bytes(
            b"---\r\nname: scout\r\ndescription: Cheap lookups\r\npower: low\r\n---\r\nBe quick.\r\n")
        catalog = specialists(self.base)
        self.assertEqual(catalog["scout"]["power"], "low")  # no stray "\r" on the value
        self.assertEqual(catalog["scout"]["instructions"], "Be quick.")

    def test_every_shipped_specialist_loads(self):
        self.assertGreaterEqual(len(CATALOG), 10)
        for name, spec in CATALOG.items():
            self.assertIn(spec["power"], ("low", "mid", "high"), name)
            self.assertTrue(spec["instructions"], name)

    def test_malformed_plans_raise_valueerror_not_crash(self):
        ok = {"id": "a", "specialist": "scout", "objective": "look"}
        for broken in (
                "not a dict",
                {"subtasks": ["a string"]},
                {"subtasks": [dict(ok, depends_on="s1")]},
                {"subtasks": [dict(ok, owned_paths="src")]},
                {"subtasks": [dict(ok, objective="")]},
                {"subtasks": [{k: v for k, v in ok.items() if k != "objective"}]},
                {"subtasks": [dict(ok, mode="root")]},
                {"subtasks": [dict(ok, power="ultra")]},
                {"subtasks": [dict(ok, provider="gemini")]},
                {"subtasks": [dict(ok, id="bad id!")]},
                {"subtasks": [ok, dict(ok)]},  # duplicate id
                {"subtasks": []},
                {"subtasks": [dict(ok, id=f"s{i}") for i in range(21)]}):
            with self.assertRaises(ValueError, msg=str(broken)[:60]):
                validate_plan(json.loads(json.dumps(broken)), CATALOG)

    def test_plan_ids_cannot_escape_the_plans_folder(self):
        orch = Orchestrator(Store(self.base), Memory(self.base), {"workspace_roots": ["."]}, ROOT)
        for bad in ("../tasks", "plan-zzzzzzzz", "plan-1234567", "", "plan-12345678/../x"):
            with self.assertRaises(ValueError):
                orch.load(bad)

    def test_blocked_plan_stays_blocked_and_reports_each_step(self):
        orch = Orchestrator(Store(self.base), Memory(self.base), {"workspace_roots": ["."]}, ROOT)
        plan = {"summary": "s", "subtasks": [
            {"id": "a", "specialist": "scout", "objective": "one"},
            {"id": "b", "specialist": "scout", "objective": "two", "depends_on": ["a"]}]}
        record = orch.create("goal", plan=plan)

        def fail(store, task_id, *_):
            store.transition(task_id, "queued", "running")
            store.transition(task_id, "running", "failed")
            return {"task_id": task_id, "state": "failed", "artifacts": "", "answer": None}

        with patch("jarvis.orchestrator.run_task", side_effect=fail) as run:
            result = orch.run(record["id"])
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(run.call_count, 1)  # b never starts behind a failed a
            orch.run(record["id"])
            self.assertEqual(run.call_count, 1)  # and a blocked plan isn't silently retried


# ---------------------------------------------------------------- brain lane pipeline
FLAKY_APP_SERVER = r'''
import json, os, sys
def send(m): print(json.dumps(m), flush=True)
turns = os.path.exists("seen-first-turn")  # survives the restart the lane performs
for line in sys.stdin:
    m = json.loads(line)
    meth, rid = m.get("method"), m.get("id")
    if meth == "initialize":
        send({"id": rid, "result": {}})
    elif meth == "thread/start":
        send({"id": rid, "result": {"thread": {"id": "t"}}})
    elif meth == "turn/start":
        first = not turns
        open("seen-first-turn", "w").close()
        turns = True
        if first:
            # The first turn errors out but keeps talking afterwards, as a real server can.
            send({"method": "error", "params": {"error": "boom", "willRetry": False}})
            send({"method": "item/agentMessage/delta",
                  "params": {"itemId": "old", "delta": "STALE ANSWER"}})
            send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
        else:
            send({"method": "item/agentMessage/delta",
                  "params": {"itemId": "new", "delta": "Fresh answer."}})
            send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
'''


class ChatGPTLaneRecoveryTests(Temp):
    def test_failed_turn_never_leaks_into_the_next_answer(self):
        script = self.base / "flaky.py"
        script.write_text(FLAKY_APP_SERVER, encoding="utf-8")
        lane = ChatGPTLane({}, self.base, timeout=20, command=[sys.executable, str(script)])
        self.addCleanup(lane.close)
        with self.assertRaises(RuntimeError):
            lane.ask("first")
        self.assertEqual(lane.ask("second"), "Fresh answer.")  # was "STALE ANSWERFresh answer."

    def test_missing_cli_is_a_clean_runtime_error(self):
        lane = ChatGPTLane({}, self.base)
        with patch("jarvis.chatgpt.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                lane.ask("hi")


# ---------------------------------------------------------------- console HTTP pipeline
class HttpCase(Temp):
    def setUp(self):
        super().setUp()
        self.console = Console(CONFIG, ROOT, self.base)
        handler = type("H", (Handler,), {"console": self.console,
                                          "hosts": ("127.0.0.1", "localhost")})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        hdrs = {"X-JARVIS": "1", **(headers or {})}
        conn.putrequest(method, path, skip_host=bool(host))
        if host:
            conn.putheader("Host", host)
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None
                                                     else b"")
        if "Content-Length" not in hdrs:
            hdrs["Content-Length"] = str(len(data))
        for key, value in hdrs.items():
            conn.putheader(key, value)
        conn.endheaders(data)
        res = conn.getresponse()
        raw = res.read()
        return res.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)


class ConsoleHttpRobustnessTests(HttpCase):
    def test_negative_or_garbage_content_length_is_a_400(self):
        for length in ("-5", "abc"):
            status, body = self.request("POST", "/api/memory", b"", {"Content-Length": length})
            self.assertEqual(status, 400, length)  # used to drop the connection with a traceback
            self.assertIn("error", body)

    def test_oversized_body_is_refused_without_reading_it(self):
        status, _ = self.request("POST", "/api/memory", b"", {"Content-Length": str(10 ** 9)})
        self.assertEqual(status, 413)

    def test_json_must_be_an_object(self):
        for body in (b"[1]", b'"text"', b"12", b"{broken"):
            status, payload = self.request("POST", "/api/memory", body)
            self.assertEqual(status, 400, body)  # lists used to surface as a 500
            self.assertIn("error", payload)

    def test_post_needs_the_csrf_header_and_a_local_host(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/memory", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()
        status, _ = self.request("GET", "/api/overview", host="evil.example")
        self.assertEqual(status, 403)

    def test_static_files_cannot_escape_the_web_folder(self):
        for path in ("/../ui.py", "/..%2fui.py", "/%2e%2e/ui.py", "/fonts/../../ui.py",
                     "/index.html/../../__main__.py"):
            status, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)

    def test_every_page_asset_the_html_references_is_served(self):
        import re
        status, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        for ref in re.findall(rb'(?:src|href)="([^"#]+)"', html):
            status, _ = self.request("GET", "/" + ref.decode().split("?")[0])
            self.assertEqual(status, 200, ref)

    def test_unknown_ids_are_clean_errors(self):
        for path in ("/api/jobs/zzz", "/api/tasks/zzz", "/api/plans/plan-00000000",
                     "/api/memory/item?id=nope", "/api/export?case=nope"):
            status, body = self.request("GET", path)
            self.assertIn(status, (400, 404), path)
            self.assertIn("error", body)

    def test_capture_validates_input(self):
        for bad in ({"text": ""}, {"text": "x", "kind": "Bad Kind"},
                    {"text": "x", "importance": 7}, {"text": "x", "source_url": "javascript:1"},
                    {"text": "x", "case": "no-such-case"}):
            status, _ = self.request("POST", "/api/memory", bad)
            self.assertIn(status, (400, 404), bad)
        status, item = self.request("POST", "/api/memory", {"text": "kept", "tags": ["a"]})
        self.assertEqual((status, item["body"]), (200, "kept"))


# ---------------------------------------------------------------- credits pipeline
class CreditsResilienceTests(Temp):
    def test_one_provider_with_a_strange_reply_does_not_blank_the_panel(self):
        credits = Credits(self.base, {})
        with patch.object(Credits, "_deepseek", side_effect=ValueError("could not convert")), \
                patch.object(Credits, "_openrouter", side_effect=KeyError("data")):
            readings = {r["id"]: r for r in credits.all(refresh=True)}
        self.assertEqual(readings["deepseek"]["status"], "error")
        self.assertEqual(readings["openrouter"]["status"], "error")
        self.assertEqual(len(readings), 9)  # the others still reported

    def test_manual_entries_round_trip_and_validate(self):
        credits = Credits(self.base, {})
        credits.set_manual("glm", 12.5, 20, "USD", "topped up")
        reading = next(r for r in credits.all(refresh=True) if r["id"] == "glm")
        self.assertEqual((reading["source"], reading["headline"]["value"]), ("manual", 12.5))
        with self.assertRaises(ValueError):
            credits.set_manual("nonsense", 1)
        credits.set_manual("glm", "")  # blank clears it
        self.assertEqual(credits.manual(), {})

    def test_http_failures_become_readable_errors(self):
        import urllib.error
        from jarvis import credits as mod
        err = urllib.error.HTTPError("https://api.deepseek.com/x", 401, "no", {}, None)
        with patch.object(mod.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401.*check the key"):
                mod._get_json("https://api.deepseek.com/x", {})
        with patch.object(mod.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaisesRegex(RuntimeError, "Couldn't reach api.deepseek.com"):
                mod._get_json("https://api.deepseek.com/x", {})


# ---------------------------------------------------------------- device pipelines
class DeviceTests(unittest.TestCase):
    def test_zero_percent_free_memory_is_flagged_not_ignored(self):
        remote = {"reachable": True, "ssh": True, "ip": "1.2.3.4", "mem_avail_pct": 0.0,
                  "lan_mouse": {"running": True, "listening": True, "fallback": "inactive"}}
        local = {"lan_mouse": {"running": True, "listening": True, "peers": ["1.2.3.4"]}}
        level, lines = probe.verdict(local, remote, "1.2.3.4")
        self.assertEqual(level, "warn")
        self.assertTrue(any("memory is tight" in line for line in lines))

    def test_verdict_levels(self):
        up = {"lan_mouse": {"running": True, "listening": True, "peers": ["9.9.9.9"]}}
        remote = {"reachable": True, "ssh": True, "ip": "9.9.9.9", "mem_avail_pct": 50,
                  "wifi_power_save": "off",
                  "lan_mouse": {"running": True, "listening": True, "fallback": "inactive"}}
        self.assertEqual(probe.verdict(up, remote, "9.9.9.9")[0], "up")
        self.assertEqual(probe.verdict(up, {**remote, "ip": "9.9.9.8"}, "9.9.9.9")[0], "warn")
        self.assertEqual(probe.verdict(up, {"reachable": False, "error": "asleep"}, "9.9.9.9")[0],
                         "down")
        self.assertEqual(probe.verdict({"lan_mouse": {"running": False}}, None, None)[0], "down")

    def test_remote_summary_survives_garbage(self):
        out = probe.remote_summary(probe.parse_kv("host=zen\nmem_total_kb=abc\nlm_unit=active\n"))
        self.assertEqual(out["hostname"], "zen")
        self.assertIsNone(out["mem_avail_pct"])
        self.assertTrue(out["lan_mouse"]["running"])

    def test_netstat_parsing_picks_only_the_daemons_sockets(self):
        text = ("  UDP    0.0.0.0:4242    *:*    111\n"
                "  UDP    172.1.43.16:51000    172.1.42.215:4242    111\n"
                "  UDP    0.0.0.0:5353    *:*    222\n")
        self.assertEqual(probe.parse_netstat_udp(text, 111), (True, ["172.1.42.215"]))
        self.assertEqual(probe.parse_netstat_udp(text, 222), (False, []))

    def test_lan_mouse_config_rejects_bad_peers(self):
        good = dict(peer_name="archmain", peer_ips=["172.1.42.215"], position="left",
                    peer_fingerprint=FINGERPRINT)
        self.assertIn('position = "left"', lan_mouse_config(**good))
        for key, bad in (("peer_ips", ["999.1.1.1"]), ("peer_ips", ["1.2.3"]),
                         ("peer_name", 'x"; rm -rf /'), ("position", "diagonal"),
                         ("peer_fingerprint", "AB:CD")):
            with self.assertRaises(ValueError, msg=f"{key}={bad}"):
                lan_mouse_config(**{**good, key: bad})


# ---------------------------------------------------------------- file transfer pipeline
class TransferTests(Temp):
    def fake_run(self, listing=""):
        calls = []

        def run(args, timeout, **_):
            calls.append(args)
            out = listing if args[-1].startswith("mkdir -p -- JARVIS-Drop && ls") else ""
            return subprocess.CompletedProcess(args, 0, out, "")

        return calls, run

    def test_names_are_flattened_and_neutralised(self):
        from jarvis.transfer import safe_name
        for raw, want in (("../../etc/passwd", "passwd"), ("C:\\Users\\x\\a b.png", "a b.png"),
                          ("-rf", "rf"), (".bashrc", "bashrc"), ("a;rm -rf ~.txt", "a_rm -rf _.txt"),
                          ("", "file"), (None, "file")):
            self.assertEqual(safe_name(raw), want)
        self.assertLessEqual(len(safe_name("x" * 500)), 200)

    def test_send_never_overwrites_and_uses_strict_ssh(self):
        from jarvis import transfer
        src = self.base / "shot.png"
        src.write_bytes(b"png")
        calls, run = self.fake_run("shot.png\nnotes.txt\nsub/\n")
        with patch.object(transfer, "_run", run), \
                patch.object(transfer.shutil, "which", side_effect=lambda n: n):
            out = transfer.send("jdoe@172.1.42.215", "~/.ssh/key", src)
        self.assertEqual(out["sent"], "shot (2).png")  # shot.png already waiting there
        scp = calls[-1]
        self.assertEqual(scp[0], "scp")
        self.assertIn("StrictHostKeyChecking=yes", scp)
        self.assertIn("BatchMode=yes", scp)
        self.assertIn("--", scp)
        self.assertEqual(scp[-1], "jdoe@172.1.42.215:JARVIS-Drop/shot (2).png")

    def test_send_and_fetch_reject_bad_input(self):
        from jarvis import transfer
        with self.assertRaises(ValueError):
            transfer.send("-oProxyCommand=x@y", None, self.base)
        with self.assertRaises(ValueError):
            transfer.send("jdoe@host", None, self.base / "missing")
        calls, run = self.fake_run("ok.txt\n")
        with patch.object(transfer, "_run", run), \
                patch.object(transfer.shutil, "which", side_effect=lambda n: n):
            with self.assertRaises(ValueError):
                transfer.fetch("jdoe@host", None, "../../secret", self.base)  # not in the inbox
            self.assertEqual(transfer.fetch("jdoe@host", None, "ok.txt", self.base)["fetched"],
                             "ok.txt")

    def test_ssh_device_selection(self):
        from jarvis.transfer import ssh_device
        self.assertEqual(ssh_device(CONFIG)["target"], CONFIG["devices"]["zenbook"]["target"])
        with self.assertRaises(ValueError):
            ssh_device(CONFIG, "dell")  # local, not SSH
        with self.assertRaises(ValueError):
            ssh_device({"devices": {}})


class UploadEndpointTests(HttpCase):
    def test_upload_is_streamed_to_the_sender_with_a_safe_name(self):
        seen = {}

        def fake_send(target, identity, path, name):
            seen.update(target=target, name=name, data=Path(path).read_bytes())
            return {"sent": name, "bytes": len(seen["data"]), "to": "x"}

        with patch("jarvis.transfer.send", fake_send):
            status, body = self.request("POST", "/api/send?name=..%2F..%2Fa%20b.png", b"PNGdata")
        self.assertEqual((status, body["sent"]), (200, "a b.png"))
        self.assertEqual(seen["data"], b"PNGdata")
        self.assertEqual(seen["target"], CONFIG["devices"]["zenbook"]["target"])

    def test_upload_rejects_empty_and_unauthenticated(self):
        status, _ = self.request("POST", "/api/send?name=x", b"")
        self.assertEqual(status, 400)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/send?name=x", b"abc")  # no X-JARVIS header
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()


if __name__ == "__main__":
    unittest.main()
