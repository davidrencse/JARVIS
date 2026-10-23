import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from jarvis import credits as credits_mod
from jarvis.credits import Credits, claude_usage_rows, codex_rate_limits
from jarvis.ui import Console, Handler

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "jarvis.config.json").read_text(encoding="utf-8"))


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()


class CreditTests(Temp):
    def test_codex_rate_limits_takes_newest_event(self):
        log = self.base / "rollout.jsonl"
        older = {"timestamp": "2026-10-20T10:00:00Z", "payload": {"rate_limits": {
            "primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": 1}}}}
        newer = {"timestamp": "2026-10-20T12:00:00Z", "payload": {"info": {}, "rate_limits": {
            "primary": {"used_percent": 40.0, "window_minutes": 300, "resets_at": 2},
            "secondary": {"used_percent": 22.0, "window_minutes": 10080, "resets_at": 3}}}}
        log.write_text("\n".join([json.dumps(older), "not json", json.dumps(newer),
                                  json.dumps({"type": "other"})]), encoding="utf-8")
        limits = codex_rate_limits(log)
        self.assertEqual(limits["primary"]["used_percent"], 40.0)
        self.assertEqual(limits["secondary"]["used_percent"], 22.0)
        self.assertEqual(limits["_logged_at"],
                         datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp())

    def test_claude_rows_and_dedupe(self):
        now = datetime.now(timezone.utc).isoformat()
        msg = {"type": "assistant", "timestamp": now, "message": {"id": "m1", "usage": {
            "input_tokens": 2, "cache_read_input_tokens": 100, "output_tokens": 50}}}
        log = self.base / "projects" / "p" / "s.jsonl"
        log.parent.mkdir(parents=True)
        # Claude Code repeats a message's usage on every content block.
        log.write_text("\n".join([json.dumps(msg), json.dumps(msg)]), encoding="utf-8")
        self.assertEqual(claude_usage_rows(log)[0][1:], claude_usage_rows(log)[1][1:])
        reading = {"facts": [], "meters": []}
        with patch.object(credits_mod, "HOME", self.base / "home"):
            (self.base / "home" / ".claude").mkdir(parents=True)
            (self.base / "projects").rename(self.base / "home" / ".claude" / "projects")
            Credits(self.base)._claude(reading, ())
        self.assertEqual(reading["headline"]["value"], 50)
        self.assertEqual(reading["source"], "local")

    def test_manual_entry_and_unconfigured_states(self):
        with patch.object(credits_mod, "HOME", self.base / "empty"), \
                patch.dict("os.environ", {}, clear=True):
            c = Credits(self.base, CONFIG)
            readings = {r["id"]: r for r in c.all()}
            self.assertEqual(readings["zai"]["status"], "unconfigured")
            self.assertEqual(readings["deepseek"]["headline"], None)
            c.set_manual("zai", "12.5", total="20", unit="USD", note="coding plan")
            zai = {r["id"]: r for r in c.all()}["zai"]
            self.assertEqual((zai["source"], zai["headline"]["value"]), ("manual", 12.5))
            self.assertEqual(zai["meters"][0]["pct"], 37.5)
            c.set_manual("zai", "")
            self.assertEqual({r["id"]: r for r in c.all()}["zai"]["source"], "none")
            with self.assertRaises(ValueError):
                c.set_manual("nope", 1)


class GraphTests(Temp):
    def test_system_graph_follows_real_imports(self):
        console = Console(CONFIG, ROOT, self.base)
        graph = console.system_graph()
        edges = {(e["src"], e["dst"]) for e in graph["edges"] if e["rel"] == "imports"}
        self.assertIn(("mod:assistant", "mod:router"), edges)
        self.assertIn(("mod:ui", "mod:memory"), edges)
        uses = {(e["src"], e["dst"]) for e in graph["edges"] if e["rel"] == "uses"}
        self.assertIn(("mod:speech", "ext:elevenlabs"), uses)
        self.assertFalse(any(src == "mod:ui" for src, _ in uses))
        ids = {n["id"] for n in graph["nodes"]}
        self.assertIn("agent:scout", ids)
        self.assertTrue(all(e["src"] in ids and e["dst"] in ids for e in graph["edges"]))

    def test_heap_graph_has_items_hubs_and_links(self):
        console = Console(CONFIG, ROOT, self.base)
        a = console.memory.add("message", "hello", tags=["inbound", "lan-mouse"])
        b = console.memory.add("reply", "hi", links=[("answers", a["id"])])
        graph = console.heap_graph()
        ids = {n["id"] for n in graph["nodes"]}
        self.assertTrue({a["id"], b["id"], "kind:message", "tag:lan-mouse"} <= ids)
        self.assertNotIn("tag:inbound", ids)  # bookkeeping tags would cluster everything
        self.assertIn((b["id"], a["id"], "answers"),
                      {(e["src"], e["dst"], e["rel"]) for e in graph["edges"]})

    def test_tree_hides_state_and_secrets(self):
        names = json.dumps(Console(CONFIG, ROOT, self.base).tree())
        self.assertNotIn("secrets.json", names)
        self.assertNotIn('"name": ".jarvis"', names)


class SaveKeyTests(Temp):
    KEY = "sk-test0123456789abcdef"

    def secrets(self):
        return json.loads((self.base / "secrets.json").read_text(encoding="utf-8"))

    def test_good_key_is_checked_merged_and_masked(self):
        from jarvis.credits import save_key
        (self.base / "secrets.json").write_text(json.dumps({"ELEVENLABS_API_KEY": "keep"}))
        balance = {"is_available": True, "balance_infos": [
            {"currency": "USD", "total_balance": "4.20", "granted_balance": "0",
             "topped_up_balance": "4.20"}]}
        with patch.object(credits_mod, "_get_json", return_value=balance) as call:
            out = save_key(self.base, "deepseek", CONFIG, secret=lambda _p: f"  {self.KEY}\n")
        self.assertEqual(call.call_args.args[1]["Authorization"], f"Bearer {self.KEY}")
        self.assertEqual(self.secrets(), {"ELEVENLABS_API_KEY": "keep", "DEEPSEEK_API_KEY": self.KEY})
        self.assertEqual((out["status"], out["balance"], out["key"]), ("ok", "4.2 USD balance", "…cdef"))
        self.assertNotIn(self.KEY, json.dumps(out))

    def test_rejected_or_junk_key_is_not_saved(self):
        from jarvis.credits import save_key
        with patch.object(credits_mod, "_get_json",
                          side_effect=RuntimeError("HTTP 401 from api.deepseek.com — check the key")):
            with self.assertRaises(ValueError):
                save_key(self.base, "deepseek", CONFIG, secret=lambda _p: self.KEY)
        with self.assertRaises(ValueError):
            save_key(self.base, "deepseek", CONFIG, secret=lambda _p: "short")
        with self.assertRaises(ValueError):
            save_key(self.base, "zai", CONFIG, secret=lambda _p: self.KEY)  # no balance endpoint
        self.assertFalse((self.base / "secrets.json").exists())

    def test_offline_check_still_saves_with_a_note(self):
        from jarvis.credits import save_key
        with patch.object(credits_mod, "_get_json",
                          side_effect=RuntimeError("Couldn't reach api.deepseek.com: timed out")):
            out = save_key(self.base, "deepseek", CONFIG, secret=lambda _p: self.KEY)
        self.assertEqual(out["status"], "unchecked")
        self.assertEqual(self.secrets()["DEEPSEEK_API_KEY"], self.KEY)


class CodexStaleTests(Temp):
    def test_passed_reset_does_not_invent_a_refill(self):
        sessions = self.base / ".codex" / "sessions" / "2026"
        sessions.mkdir(parents=True)
        past, future = time.time() - 60, time.time() + 3600
        event = {"timestamp": "2026-10-20T12:00:00Z", "rate_limits": {
            "primary": {"used_percent": 40.0, "window_minutes": 300, "resets_at": past},
            "secondary": {"used_percent": 22.0, "window_minutes": 10080, "resets_at": future}}}
        (sessions / "r.jsonl").write_text(json.dumps(event), encoding="utf-8")
        reading = {"facts": [], "meters": []}
        with patch.object(credits_mod, "HOME", self.base):
            Credits(self.base)._codex(reading, ())
        self.assertEqual(len(reading["meters"]), 1)
        week = reading["meters"][0]
        self.assertEqual(week["used"], 22.0)
        self.assertEqual(reading["headline"]["value"], 78)
        self.assertIn("current usage unknown", str(reading["facts"]))


class InvestigationTests(Temp):
    def setUp(self):
        super().setUp()
        self.c = Console(CONFIG, ROOT, self.base)

    def test_case_capture_link_search_export(self):
        case = self.c.create_case("Home lab exposure")
        self.assertEqual(case["slug"], "home-lab-exposure")
        with self.assertRaises(ValueError):
            self.c.create_case("home lab  exposure")
        a = self.c.capture({"text": "22/tcp open ssh OpenSSH 9.6", "kind": "output",
                            "title": "nmap 10.0.0.5", "importance": 0.9, "tags": ["#nmap"],
                            "source_url": "https://example.com/scan", "case": case["slug"]})
        b = self.c.capture({"text": "Router admin panel on 8443", "kind": "finding",
                            "case": case["slug"]})
        outside = self.c.capture({"text": "Unrelated grocery note"})
        self.assertIn("case:home-lab-exposure", a["tags"])
        self.assertEqual(a["meta"]["source_url"], "https://example.com/scan")
        with self.assertRaises(ValueError):
            self.c.capture({"text": "x", "source_url": "javascript:alert(1)"})
        self.c.memory.link(b["id"], a["id"], "supports")
        hits = self.c.search({"q": "", "case": case["slug"]})
        self.assertEqual({h["id"] for h in hits} - {case["id"]}, {a["id"], b["id"]})
        self.assertEqual([h["id"] for h in self.c.search({"q": "ssh", "min_importance": "0.85"})],
                         [a["id"]])
        self.assertEqual(self.c.cases()[0]["items"], 2)
        graph = self.c.heap_graph(case=case["slug"])
        ids = {n["id"] for n in graph["nodes"]}
        self.assertTrue({a["id"], b["id"]} <= ids)
        self.assertNotIn(outside["id"], ids)
        md = self.c.export(case["slug"])
        self.assertTrue(md.startswith("# Home lab exposure"))
        self.assertIn("Source: https://example.com/scan", md)
        self.assertIn(f"supports → `{a['id']}`", md)
        with self.assertRaises(LookupError):
            self.c.export("nope")

    def test_history_pairs_requests_with_replies(self):
        q = self.c.memory.add("message", "what day is it", tags=["inbound", "simple"])
        self.c.memory.add("reply", "Tuesday.", links=[("answers", q["id"])], meta={"task": "abc"})
        self.c.memory.add("message", "unanswered", tags=["inbound", "complex"])
        hist = self.c.history()
        self.assertEqual([h["text"] for h in hist], ["what day is it", "unanswered"])
        self.assertEqual((hist[0]["tier"], hist[0]["reply"]["body"]), ("simple", "Tuesday."))
        self.assertIsNone(hist[1]["reply"])

    def test_plan_view_reads_real_step_state(self):
        from jarvis.ui import _plan_view
        record = {"id": "plan-0000abcd", "goal": "g", "state": "planned",
                  "subtasks": [{"id": "s1", "specialist": "researcher", "objective": "look",
                                "acceptance_criteria": ["cites sources"]},
                               {"id": "s2", "specialist": "reviewer", "objective": "check",
                                "depends_on": ["s1"]}],
                  "results": {"s1": {"state": "finished", "answer": "found it"}}, "tasks": {}}
        view = _plan_view(record, full=True)
        self.assertEqual([s["state"] for s in view["steps"]], ["finished", "pending"])
        self.assertEqual(view["steps"][0]["acceptance"], ["cites sources"])
        self.assertEqual(view["steps"][0]["answer"], "found it")


class HttpTests(Temp):
    def setUp(self):
        super().setUp()
        handler = type("H", (Handler,), {"console": Console(CONFIG, ROOT, self.base),
                                          "hosts": ("127.0.0.1", "localhost")})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers=headers or {},
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as exc:
            with exc:  # an HTTPError is the response; closing it keeps the socket from leaking
                return exc.code, exc.read()

    def test_static_and_traversal(self):
        status, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b"JARVIS", body)
        self.assertEqual(self.call("/../jarvis.config.json")[0], 404)
        self.assertEqual(self.call("/%2e%2e/jarvis.config.json")[0], 404)

    def test_post_needs_header_and_host(self):
        self.assertEqual(self.call("/api/memory", {"text": "x"})[0], 403)
        self.assertEqual(self.call("/api/overview", headers={"Host": "evil.example"})[0], 403)
        status, body = self.call("/api/memory", {"text": "remember the console", "tags": ["ui"]},
                                 {"X-JARVIS": "1"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["id"].startswith("note:"))

    def test_ask_runs_in_background(self):
        fake = {"tier": "simple", "reasons": [], "reply": "It's Tuesday.", "memory": []}
        with patch("jarvis.assistant.ask", return_value=fake):
            status, body = self.call("/api/ask", {"text": "what day is it"}, {"X-JARVIS": "1"})
            self.assertEqual(status, 200)
            job = json.loads(body)
            for _ in range(50):
                job = json.loads(self.call(f"/api/jobs/{job['id']}")[1])
                if job["state"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual((job["state"], job["result"]["reply"]), ("done", "It's Tuesday."))
        self.assertEqual(self.call("/api/jobs/missing")[0], 404)
        self.assertEqual(self.call("/api/ask", {"text": " "}, {"X-JARVIS": "1"})[0], 400)


if __name__ == "__main__":
    unittest.main()
