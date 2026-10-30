import json
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock
from datetime import date, datetime
from pathlib import Path

from jarvis.assistant import ask
from jarvis.chatgpt import Brain, ChatGPTLane, make_lane
from jarvis.memory import Memory
from jarvis.store import Store
from jarvis.vault import (Toolbox, Vault, VaultTools, is_vault_request, links_in, moment,
                          recall, split_frontmatter, _PinnedHTTPHandler,
                          _PinnedHTTPSHandler, _Guarded, _connect_pinned, _public)
from jarvis.vault_mcp import handle

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 14, 5)

FILES = {
    "CLAUDE.md": "# CLAUDE.md — vault map\n\nRead this first. Example: `[[not-a-link]]`.\n",
    "MEMORY.md": "# MEMORY.md\n\n## Owner\n\n- Student at State U, studying [[operating-systems|OS]].\n"
                 "- Learns Spanish.\n\n## Vault\n\n- Git-backed.\n",
    ".obsidian/daily-notes.json": json.dumps({"format": "YYYY/MM/YYYY-MM-DD", "folder": "daily",
                                              "template": "templates/daily-note"}),
    "templates/daily-note.md": "---\ntitle: \"{{date:YYYY-MM-DD}}\"\ntype: daily\ntags: [daily]\n"
                               "created: \"{{date:YYYY-MM-DD}}\"\nupdated: \"{{date:YYYY-MM-DD}}\"\n"
                               "weekday: \"{{date:dddd}}\"\n---\n\n# {{date:dddd}}, {{date:YYYY-MM-DD}}"
                               "\n\n## Priorities\n\n1. - [ ] \n\n## Log\n\n- Done:\n- Didn't:\n\n"
                               "## References\n",
    "daily/2026/10/2026-10-21.md": "---\ntitle: \"2026-10-21\"\ntype: daily\ntags: [daily]\n"
                                   "created: \"2026-10-21\"\nupdated: \"2026-10-20\"\n---\n\n"
                                   "# Wednesday\n\n## Priorities\n\n1. - [ ] Start the OS cheat sheet\n"
                                   "2. - [ ] \n\n## Log\n\n- Done:\n- Didn't:\n\n## References\n",
    "courses/operating-systems.md": "---\ntitle: Operating Systems\ntype: course\n"
                                    "tags: [school, computer-science]\ncreated: 2026-10-05\n"
                                    "updated: 2026-10-15\nstatus: active\naliases: [OS class]\n---\n\n"
                                    "# Operating Systems\n\n## Assessments\n\n- [ ] Midterm 1 — "
                                    "October 27, 2026. Covers page replacement and scheduling.\n\n"
                                    "## Assignments\n\n- [ ] Assignment 2\n",
    "projects/drone-blueprint.md": "---\ntitle: Drone Blueprint\ntype: project\ntags: [project]\n"
                                   "created: 2026-10-21\nupdated: 2026-10-21\nstatus: active\n---\n\n"
                                   "# Drone Blueprint\n\n## Next step\n\n- [ ] Decide what the drone "
                                   "is for\n\n## Related\n\n[[operating-systems]]\n",
    "notes/emotion.md": "---\ntitle: Emotion\ntype: note\ntags: [psychology]\ncreated: 2026-06-15\n"
                        "updated: 2026-10-15\n---\n\n# Emotion\n\n| Topic | Note |\n| --- | --- |\n"
                        "| amygdala | [[amygdala-fear\\|Amygdala and fear]] |\n",
    "notes/amygdala-fear.md": "---\ntitle: Amygdala and fear\ntype: note\ntags: [psychology]\n"
                              "created: 2026-06-15\nupdated: 2026-10-15\n---\n\n# Amygdala and fear\n\n"
                              "The amygdala learns fear; see [[emotion]] and ![[brain.png]].\n",
    "notes/Bad Name.md": "---\ntitle: Bad\ntype: weird\ntags: []\ncreated: 2026-06-15\n---\n\n"
                         "Links to [[missing-note]] and [[2026-11-02]].\n",
    "reference/lecture-emotion.md": "---\ntitle: Lecture Emotion\ntype: reference\ntags: []\n"
                                    "created: 2026-06-15\nupdated: 2026-06-15\n---\n\n"
                                    "amygdala amygdala fear fear transcript\n",
    "calendar.md": "---\ntitle: Calendar\ntype: index\ntags: [index]\ncreated: 2026-10-21\n"
                   "updated: 2026-10-21\n---\n\n## Exams and deadlines\n\n"
                   "- **2026-10-27 14:00–15:50** — OS Midterm 1 — [[operating-systems|Operating Systems]]\n"
                   "- **2026-12-20 (all day)** — Far away\n\n## Upcoming\n\n"
                   "### Wed 2026-10-21 · [[2026-10-21]]\n\n- 15:00–17:00 OS office hours — "
                   "https://example.zoom.us/j/1 (overlaps)\n\n### Fri 2026-10-23 · [[2026-10-23]]\n\n"
                   "- 10:00 Not today or tomorrow\n",
    "archive/old.md": "---\ntitle: Old amygdala\ntype: note\n---\n\namygdala\n",
    "attachments/brain.png": "png",
}


class VaultCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name, "Vault")
        for rel, text in FILES.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")
        self.state = Path(temp.name, "state")
        self.vault = Vault(self.root, self.state, {"write": True})

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")


class ParsingTests(unittest.TestCase):
    def test_frontmatter_subset(self):
        fields, body = split_frontmatter("---\ntitle: \"A: b\"\ntags: [x, y]\naliases:\n  - One\n"
                                         "  - 'Two'\nstatus: active # comment\n---\nbody\n")
        self.assertEqual(fields, {"title": "A: b", "tags": ["x", "y"], "aliases": ["One", "Two"],
                                  "status": "active"})
        self.assertEqual(body, "body\n")
        self.assertEqual(split_frontmatter("no fm"), (None, "no fm"))

    def test_links_skip_code_and_unescape_table_pipes(self):
        text = "[[a]] [[b\\|B]] ![[c.png]] [[d#Heading|D]] [[#local]] `[[e]]`\n```\n[[f]]\n```\n"
        self.assertEqual(links_in(text), ["a", "b", "c.png", "d"])

    def test_moment_tokens(self):
        self.assertEqual(moment("YYYY/MM/YYYY-MM-DD dddd", datetime(2026, 10, 21)),
                         "2026/10/2026-10-21 Wednesday")

    def test_vault_requests(self):
        for text in ("Jarvis, remember that the OS midterm is open note",
                     "note that I finished assignment 2", "what do my notes say about the amygdala",
                     "log that I went to the gym", "add this to my obsidian vault",
                     "save this to my second brain", "write that down"):
            self.assertTrue(is_vault_request(text), text)
        for text in ("what's the weather", "open spotify", "how do notes work in music",
                     "tell me a joke"):
            self.assertFalse(is_vault_request(text), text)


class ReadTests(VaultCase):
    def test_index_skips_archive_templates_attachments_and_follows_edits(self):
        paths = {n["path"] for n in self.vault.notes()}
        self.assertIn("notes/emotion.md", paths)
        self.assertFalse(any(p.startswith(("archive/", "templates/")) for p in paths))
        (self.root / "notes" / "emotion.md").unlink()
        (self.root / "notes" / "new-idea.md").write_text("# New idea\n\nzebra crossing\n", "utf-8")
        self.vault.refresh(force=True)
        self.assertEqual([h["path"] for h in self.vault.search("zebra")], ["notes/new-idea.md"])
        self.assertNotIn("notes/emotion.md", {n["path"] for n in self.vault.notes()})

    def test_search_ranks_titles_and_resolves_names(self):
        hits = self.vault.search("amygdala fear")
        self.assertEqual(hits[0]["path"], "notes/amygdala-fear.md")
        self.assertIn("amygdala", hits[0]["snippet"].lower())
        self.assertEqual(self.vault.search("the and of"), [])  # stopwords only
        for ref in ("courses/operating-systems.md", "operating-systems", "Operating Systems",
                    "os class", "[[operating-systems|OS]]"):
            self.assertEqual(self.vault.resolve(ref), "courses/operating-systems.md", ref)
        self.assertIsNone(self.vault.resolve("../outside"))
        self.assertIsNone(self.vault.resolve(".obsidian/daily-notes"))
        self.assertEqual(self.vault.read("Emotion")["path"], "notes/emotion.md")
        with self.assertRaises(ValueError):
            self.vault.read("nope")

    def test_backlinks_include_escaped_table_links(self):
        hits = {h["path"] for h in self.vault.backlinks("amygdala-fear")["backlinks"]}
        self.assertEqual(hits, {"notes/emotion.md"})

    def test_recall_is_bounded_abstains_and_skips_verbatim_sources(self):
        lines = self.vault.context("what did I write about the amygdala and fear?")
        self.assertTrue(lines and lines[0].startswith("[vault:notes/amygdala-fear.md]"))
        self.assertFalse(any("reference/" in l for l in lines))
        self.assertLessEqual(sum(map(len, lines)), 900)
        self.assertEqual(self.vault.context("what's the weather like today?"), [])
        self.assertEqual(self.vault.context("amygdala"), [])  # too short to be a question
        self.assertEqual(recall({}, self.state, "amygdala and fear please"), [])  # no vault set

    def test_world_brief(self):
        brief = self.vault.world(today=TODAY)
        for expected in ("Student at State U, studying OS.", "Current courses: Operating Systems",
                         "Drone Blueprint (next: Decide what the drone is for)",
                         "2026-10-27 14:00–15:50 — OS Midterm 1 — Operating Systems",
                         "today 15:00–17:00 OS office hours;", "Start the OS cheat sheet"):
            self.assertIn(expected, brief)
        for absent in ("Far away", "Not today or tomorrow", "zoom.us", "Git-backed"):
            self.assertNotIn(absent, brief)
        self.assertLessEqual(len(self.vault.world(max_chars=200, today=TODAY)), 200)


class WriteTests(VaultCase):
    def test_log_appends_under_log_and_bumps_updated(self):
        result = self.vault.log("Finished\nassignment 2", when=NOW)
        self.assertEqual(result, {"path": "daily/2026/10/2026-10-21.md",
                                  "added": "- 14:05 Finished assignment 2", "created": False})
        text = self.read("daily/2026/10/2026-10-21.md")
        self.assertIn("- Didn't:\n- 14:05 Finished assignment 2\n\n## References", text)
        self.assertIn('updated: "2026-10-21"', text)
        audit = [json.loads(l) for l in (self.state / "vault-writes.jsonl").read_text().splitlines()]
        self.assertEqual(audit[0]["action"], "log")

    def test_log_creates_daily_note_from_the_template(self):
        result = self.vault.log("First thing", when=datetime(2026, 10, 25, 9, 30))
        self.assertTrue(result["created"])
        text = self.read("daily/2026/10/2026-10-25.md")
        self.assertIn('weekday: "Sunday"', text)
        self.assertIn("# Sunday, 2026-10-25", text)
        self.assertIn("- Didn't:\n- 09:30 First thing\n", text)
        self.assertNotIn("{{", text)

    def test_crlf_files_keep_their_line_endings(self):
        path = self.root / "notes" / "crlf.md"
        path.write_bytes(b"---\r\ntitle: Crlf\r\ntype: note\r\ntags: []\r\ncreated: 2026-01-01\r\n"
                         b"updated: 2026-01-01\r\n---\r\n\r\n# Crlf\r\n\r\n## Ideas\r\n\r\n- one\r\n")
        self.vault.refresh(force=True)
        self.vault.append("crlf", "- two", "Ideas", when=NOW)
        data = path.read_bytes()
        self.assertIn(b"updated: 2026-10-21\r\n", data)
        self.assertIn(b"- one\r\n- two\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_capture_uses_the_schema_and_refuses_duplicates(self):
        result = self.vault.capture("Page Replacement: LRU vs FIFO", "Belady's anomaly hits FIFO.",
                                    ["computer-science", "OS Stuff", "bad tag!"], when=NOW)
        self.assertEqual(result["path"], "notes/page-replacement-lru-vs-fifo.md")
        fields, body = split_frontmatter(self.read(result["path"]))
        self.assertEqual(fields, {"title": "Page Replacement: LRU vs FIFO", "type": "note",
                                  "tags": ["computer-science", "os-stuff", "bad-tag"],
                                  "created": "2026-10-21", "updated": "2026-10-21"})
        self.assertTrue(body.startswith("\n# Page Replacement: LRU vs FIFO\n\nBelady"))
        self.assertIn("[[page-replacement-lru-vs-fifo|Page Replacement: LRU vs FIFO]]",
                      self.read("daily/2026/10/2026-10-21.md"))  # no orphan notes
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.vault.capture("page replacement lru vs fifo", "again")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.vault.capture("Operating Systems", "same title as a course")
        project = self.vault.capture("Lab Server", "Build it.", kind="project", when=NOW)
        self.assertIn("status: active", self.read(project["path"]))
        with self.assertRaises(ValueError):
            self.vault.capture("x", "y", kind="secret")

    def test_append_to_section_new_section_or_end(self):
        self.vault.append("operating-systems", "- [ ] Assignment 3", "Assignments", when=NOW)
        self.vault.append("Operating Systems", "Office hours moved.", "Notes", when=NOW)
        self.vault.append("drone-blueprint", "- [ ] Pick motors", "Next step", when=NOW)
        os_text = self.read("courses/operating-systems.md")
        self.assertIn("- [ ] Assignment 2\n- [ ] Assignment 3\n\n## Notes\n\nOffice hours moved.\n",
                      os_text)
        self.assertIn("updated: 2026-10-21", os_text)
        self.assertIn("- [ ] Decide what the drone is for\n- [ ] Pick motors\n\n## Related",
                      self.read("projects/drone-blueprint.md"))
        (self.root / "notes" / "wrap.md").write_text(
            "# Wrap\n\n## Next\n\n- [ ] A long item\n      that wraps\n\n## End\n", "utf-8")
        self.vault.refresh(force=True)
        self.vault.append("wrap", "- [ ] Second", "Next", when=NOW)
        self.assertIn("      that wraps\n- [ ] Second\n\n## End", self.read("notes/wrap.md"))

    def test_refusals(self):
        for ref in ("calendar", "../outside", "archive/old", "templates/daily-note"):
            with self.assertRaises(ValueError, msg=ref):
                self.vault.append(ref, "text")
        with self.assertRaises(ValueError):
            self.vault.append("emotion", "   ")
        with self.assertRaises(ValueError):
            self.vault.append("emotion", "x" * 5000)
        path = self.root / "notes" / "emotion.md"
        path.write_text(path.read_text("utf-8") + "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> x\n",
                        "utf-8")
        with self.assertRaisesRegex(RuntimeError, "merge conflicts"):
            self.vault.append("emotion", "more")
        locked = Vault(self.root, self.state, {"write": False})
        with self.assertRaisesRegex(ValueError, "writes are off"):
            locked.log("nope", when=NOW)
        self.assertEqual(VaultTools(locked).names(), {"vault_search", "vault_read",
                                                      "vault_backlinks", "vault_world"})

    def test_write_setting_applies_while_running(self):
        from jarvis.vault import vault_for
        config = {"vault": {"path": str(self.root), "write": True}}
        vault = vault_for(config, self.state / "live")
        tools = VaultTools(vault)
        self.assertIn("vault_log", tools.names())
        config["vault"]["write"] = False  # what the console's settings.write does
        self.assertIs(vault_for(config, self.state / "live"), vault)
        self.assertFalse(vault.writable)
        self.assertNotIn("vault_log", tools.names())
        with self.assertRaisesRegex(ValueError, "writes are off"):
            vault.log("nope", when=NOW)
        config["vault"] = {"path": str(self.root), "write": True}  # a reloaded config
        vault_for(config, self.state / "live")
        self.assertIn("vault_log", tools.names())

    def test_index_pages_rank_below_the_note_they_list(self):
        (self.root / "courses").mkdir(exist_ok=True)
        (self.root / "courses" / "_index.md").write_text(
            "---\ntitle: Courses\ntype: index\n---\n- [[kernels]] kernels kernels scheduling\n", "utf-8")
        (self.root / "courses" / "kernels.md").write_text(
            "---\ntitle: Kernels\ntype: course\n---\nScheduling and kernels.\n", "utf-8")
        hits = [h["path"] for h in self.vault.search("kernels scheduling")]
        self.assertLess(hits.index("courses/kernels.md"), hits.index("courses/_index.md"))

    def test_world_brief_keeps_deadlines_when_trimmed(self):
        with mock.patch.object(Vault, "_calendar", return_value=(["2026-11-02 OS exam"], [])), \
                mock.patch.object(Vault, "_section_bullets", return_value=["x" * 200] * 6):
            brief = self.vault.world(max_chars=400)
        self.assertLessEqual(len(brief), 400)
        self.assertIn("Deadlines in the next 14 days: 2026-11-02 OS exam.", brief)


class HealthTests(VaultCase):
    def test_reports_against_the_vaults_own_rules(self):
        (self.root / "notes" / "lonely.md").write_text(
            "---\ntitle: Lonely\ntype: note\ntags: []\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
            "---\n\nNobody links here.\n", "utf-8")
        (self.root / "projects" / "old.md").write_text(
            "---\ntitle: Old\ntype: project\ntags: []\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
            "status: active\n---\n\n[[lonely]] is not lonely now? No: projects count.\n", "utf-8")
        report = self.vault.health(today=TODAY)
        issues = report["issues"]
        self.assertEqual(issues["broken_links"], ["notes/Bad Name.md → [[missing-note]]"])
        self.assertEqual(issues["missing_fields"], ["notes/Bad Name.md: updated"])
        self.assertEqual(issues["unknown_type"], ["notes/Bad Name.md: weird"])
        self.assertEqual(issues["filename_style"], ["notes/Bad Name.md"])
        self.assertIn("notes/Bad Name.md", issues["orphans"])
        self.assertNotIn("notes/lonely.md", issues["orphans"])
        self.assertEqual(issues["stale_projects"], ["projects/old.md (updated 2026-01-01)"])
        self.assertNotIn("no_frontmatter", issues)  # root map files have none by design


class FetchNetworkSafetyTests(unittest.TestCase):
    def test_validation_returns_the_address_that_must_be_used(self):
        infos = [(2, 1, 6, "", ("93.184.216.34", 80)),
                 (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 80, 0, 0))]
        with mock.patch("jarvis.vault.socket.getaddrinfo", return_value=infos) as resolve:
            self.assertEqual(_public("http://example.test/page"), tuple(infos))
        resolve.assert_called_once_with("example.test", 80, proto=6)

    def test_http_handler_uses_validated_addresses_without_host_resolution(self):
        infos = [(2, 1, 6, "", ("93.184.216.34", 80))]
        addresses = {"example.test": tuple(infos)}
        handler = _PinnedHTTPHandler(addresses)
        req = urllib.request.Request("http://example.test/page")
        captured = {}

        def open_connection(factory, request):
            captured["connection"] = factory("example.test", timeout=5)
            return object()

        with mock.patch.object(handler, "do_open", side_effect=open_connection), \
             mock.patch("jarvis.vault._connect_pinned", return_value=mock.Mock()) as connect, \
             mock.patch("jarvis.vault.socket.getaddrinfo") as resolve:
            handler.http_open(req)
            captured["connection"].connect()
        connect.assert_called_once_with(tuple(infos), 5, None)
        resolve.assert_not_called()

    def test_https_handler_pins_ip_and_keeps_hostname_for_tls(self):
        infos = [(2, 1, 6, "", ("93.184.216.34", 443))]
        handler = _PinnedHTTPSHandler({"example.test": tuple(infos)})
        req = urllib.request.Request("https://example.test/page")
        captured = {}

        def open_connection(factory, request, **kwargs):
            captured["connection"] = factory("example.test", timeout=5,
                                               context=kwargs["context"])
            return object()

        raw, wrapped = mock.Mock(), mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = wrapped
        handler._context = context
        with mock.patch.object(handler, "do_open", side_effect=open_connection), \
             mock.patch("jarvis.vault._connect_pinned", return_value=raw) as connect:
            handler.https_open(req)
            captured["connection"].connect()
        connect.assert_called_once_with(tuple(infos), 5, None)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="example.test")
        self.assertIs(captured["connection"].sock, wrapped)

    def test_pinned_connect_fails_over_without_resolving_again(self):
        infos = [(2, 1, 6, "", ("93.184.216.34", 80)),
                 (2, 1, 6, "", ("203.0.113.7", 80))]
        first, second = mock.Mock(), mock.Mock()
        first.connect.side_effect = OSError("unreachable")
        with mock.patch("jarvis.vault.socket.socket", side_effect=[first, second]) as sock, \
             mock.patch("jarvis.vault.socket.getaddrinfo") as resolve:
            self.assertIs(_connect_pinned(infos, 4, None), second)
        self.assertEqual(sock.call_count, 2)
        first.close.assert_called_once()
        second.connect.assert_called_once_with(("203.0.113.7", 80))
        resolve.assert_not_called()

    def test_socket_creation_failure_fails_over_to_next_address(self):
        infos = [(10, 1, 6, "", ("2606:2800::1", 80, 0, 0)),
                 (2, 1, 6, "", ("93.184.216.34", 80))]
        second = mock.Mock()
        with mock.patch("jarvis.vault.socket.socket",
                        side_effect=[OSError("IPv6 unavailable"), second]) as sock, \
             mock.patch("jarvis.vault.socket.getaddrinfo") as resolve:
            self.assertIs(_connect_pinned(infos, 4, None), second)
        self.assertEqual(sock.call_count, 2)
        second.connect.assert_called_once_with(("93.184.216.34", 80))
        resolve.assert_not_called()

    def test_redirect_to_private_address_is_refused(self):
        private = [(2, 1, 6, "", ("127.0.0.1", 80))]
        guard = _Guarded({})
        with mock.patch("jarvis.vault.socket.getaddrinfo", return_value=private):
            with self.assertRaisesRegex(ValueError, "local or private"):
                guard.redirect_request(None, None, 302, "Found", {},
                                       "http://localhost/private")


class ToolTests(VaultCase):
    def test_tools_report_errors_instead_of_raising(self):
        memory = Memory(self.state)
        memory.add("message", "remember the drone idea", source="owner")
        memory.add("reply", "Noted.", source="jarvis")
        tools = VaultTools(self.vault, memory)
        ok, text = tools.call("vault_search", {"query": "drone blueprint"})
        self.assertTrue(ok)
        self.assertEqual(json.loads(text)[0]["path"], "projects/drone-blueprint.md")
        self.assertEqual(tools.call("vault_read", {"note": "nope"})[0], False)
        ok, text = tools.call("conversation_recent", {"turns": 5})
        self.assertEqual([t["who"] for t in json.loads(text)], ["David", "JARVIS"])
        ok, text = tools.call("vault_capture", {"title": "Idea", "body": "Body", "tags": [],
                                                "kind": "note"})
        self.assertTrue(ok, text)
        self.assertEqual(tools.call("rm_rf", {}), (False, "Unknown tool rm_rf."))
        box = Toolbox(tools, None)
        self.assertEqual(box.call("vault_world", {})[0], True)

    def test_mcp_server_is_read_only_by_default(self):
        tools = VaultTools(self.vault, write=False, channel="mcp")
        init = handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(handle(tools, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        listed = handle(tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertNotIn("vault_log", [t["name"] for t in listed["result"]["tools"]])
        call = handle(tools, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "vault_log", "arguments": {"text": "x"}}})
        self.assertTrue(call["result"]["isError"])
        found = handle(tools, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                               "params": {"name": "vault_search", "arguments": {"query": "emotion"}}})
        self.assertFalse(found["result"]["isError"])
        self.assertEqual(handle(tools, {"id": 5, "method": "nope"})["error"]["code"], -32601)


FAKE_SERVER = r'''
import json, sys
def send(m): print(json.dumps(m), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    meth, rid = m.get("method"), m.get("id")
    if meth == "initialize":
        send({"id": rid, "result": {}})
    elif meth == "thread/start":
        dev = m["params"]["developerInstructions"]
        send({"id": rid, "result": {"thread": {"id": "t1"}}})
    elif meth == "turn/start":
        prompt = m["params"]["input"][0]["text"]
        text = f"brief={'BRIEF-OK' in dev} recall={'[vault:' in prompt}."
        send({"method": "item/agentMessage/delta", "params": {"itemId": "a", "delta": text}})
        send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
'''


class WiringTests(VaultCase):
    def config(self, **actions):
        return {"actions": actions or {"enabled": False},
                "vault": {"path": str(self.root), "write": True}}

    def test_make_lane_gives_the_brain_the_vault(self):
        brain = make_lane(self.config(), self.root, self.state)
        self.assertIsInstance(brain, Brain)
        names = {s["name"] for s in brain.act.tools.specs()}
        self.assertLessEqual({"vault_search", "vault_log", "vault_capture", "conversation_recent"},
                             names)
        self.assertNotIn("open_app", names)  # actions are off in this config
        self.assertIn("Obsidian vault", brain.act._instructions())
        self.assertIn("Owner:", brain.chat._instructions())  # the world brief
        self.assertTrue(brain.wants("remember that I like chess"))
        self.assertFalse(brain.wants("open spotify"))  # no desktop tools without actions
        lane = make_lane({"actions": {"enabled": False}, "vault": {"path": str(self.root / "gone")}},
                         self.root, self.state)
        # Missing vault: no crash, and the brain keeps its own long-term memory.
        self.assertIsInstance(lane, Brain)
        names = {s["name"] for s in lane.act.tools.specs()}
        self.assertLessEqual({"memory_remember", "memory_search", "conversation_recent"}, names)
        self.assertFalse(any(n.startswith("vault_") for n in names))
        self.assertTrue(lane.wants("what did I tell you about my exam"))

    def test_brief_and_recall_reach_the_model(self):
        script = Path(self.state, "fake.py")
        script.write_text(FAKE_SERVER, encoding="utf-8")
        lane = ChatGPTLane({}, self.root, timeout=20, command=[sys.executable, str(script)],
                           context=lambda: "BRIEF-OK")
        self.addCleanup(lane.close)
        store, memory = Store(self.state), Memory(self.state)
        result = ask("what did I write about the amygdala and fear?", store, memory,
                     self.config(), self.root, lane=lane)
        self.assertEqual(result["reply"], "brief=True recall=True.")

    def test_broken_context_never_blocks_the_brain(self):
        def boom():
            raise OSError("vault unplugged")
        lane = ChatGPTLane({}, self.root, context=boom)
        self.assertEqual(lane._instructions(), lane.instructions)


if __name__ == "__main__":
    unittest.main()
