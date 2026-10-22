import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from jarvis.tasks import TaskTools, Tasks, next_dates, parse_date, parse_line
from jarvis.vault import Vault, is_vault_request

TODAY = date(2026, 10, 22)  # a Thursday
NOW = datetime(2026, 10, 22, 9, 30)

FILES = {
    ".obsidian/daily-notes.json": json.dumps({"format": "YYYY/MM/YYYY-MM-DD", "folder": "daily"}),
    "projects/drone.md": "---\ntitle: Drone\ntype: project\ntags: [project]\ncreated: 2026-10-01\n"
                         "updated: 2026-10-01\nstatus: active\n---\n\n# Drone\n\n## Next step\n\n"
                         "- [ ] Decide what it is for ⏫\n- [/] Sketch the frame 📅 2026-10-25\n"
                         "- [x] Read about FPV ✅ 2026-10-20\n\n## Related\n\nNothing.\n",
    "courses/os.md": "---\ntitle: Operating Systems\ntype: course\ntags: []\ncreated: 2026-09-01\n"
                     "updated: 2026-10-01\nstatus: active\n---\n\n## Assignments\n\n"
                     "- [ ] Assignment 2 — due October 13, 2026\n- [ ] Assignment 3 [due:: 2026-10-22]\n"
                     "- [ ] Lab 4 ⏳ 2026-10-20\n- [ ] Lab 5 📅 2026-11-30\n- [-] Old lab ❌ 2026-10-01\n"
                     "```\n- [ ] not a task, it's code\n```\n",
    "courses/ethics.md": "---\ntitle: Ethics\ntype: course\ntags: []\ncreated: 2026-06-01\n"
                         "updated: 2026-07-01\nstatus: completed\n---\n\n- [ ] Week 4 (Due June 17th)\n",
    "courses/plan.md": "---\ntitle: Study plan\ntype: study-plan\ntags: []\ncreated: 2026-10-01\n"
                       "updated: 2026-10-01\n---\n\n" + "".join(f"- [{'x' if i < 3 else ' '}] Topic {i}\n"
                                                              for i in range(20)),
    "daily/2026/10/2026-10-22.md": "---\ntitle: \"2026-10-22\"\ntype: daily\n---\n\n## Priorities\n\n"
                                   "1. - [ ] Start the cheat sheet\n2. - [ ] \n\n## Plan\n\n- [ ] Teeth\n"
                                   "- [ ] Call the bank 📅 2026-10-22\n",
    "daily/2026/10/2026-10-08.md": "---\ntitle: \"2026-10-08\"\ntype: daily\n---\n\n## Priorities\n\n"
                                   "1. - [ ] Old priority\n- [ ] Assignment 1 due September 29\n",
}


class ParseTests(unittest.TestCase):
    def test_tasks_plugin_fields(self):
        t = parse_line("  - [ ] Pick motors 🔺 🔁 every week ➕ 2026-10-01 ⏳ 2026-10-23 🛫 2026-10-21 "
                       "📅 2026-10-24 #drone ^abc123")
        self.assertEqual((t["status"], t["text"], t["priority"], t["recur"], t["created"],
                          t["scheduled"], t["start"], t["due"], t["tags"], t["indent"]),
                         ("todo", "Pick motors #drone", "highest", "every week", "2026-10-01",
                          "2026-10-23", "2026-10-21", "2026-10-24", ["drone"], 2))
        self.assertEqual(parse_line("- [x] Done thing ✅ 2026-10-20")["done"], "2026-10-20")
        self.assertEqual(parse_line("- [/] Going")["status"], "in_progress")
        self.assertEqual(parse_line("1. - [ ] Numbered priority")["text"], "Numbered priority")
        self.assertEqual(parse_line("- [ ] A [due:: 2026-10-30] [priority:: high]")["due"], "2026-10-30")
        self.assertIsNone(parse_line("- [ ] "))
        self.assertIsNone(parse_line("- plain bullet"))
        prose = parse_line("- [ ] Week 4 (Due June 17th)", date(2026, 6, 1))
        self.assertEqual((prose["due"], prose["due_from_text"]), ("2026-06-17", True))

    def test_dates_people_say(self):
        cases = {"today": "2026-10-22", "tomorrow": "2026-10-23", "friday": "2026-10-23",
                 "thursday": "2026-10-29", "next monday": "2026-11-02", "in 3 days": "2026-10-25",
                 "+2w": "2026-11-05", "Oct 30": "2026-10-30", "January 5": "2027-01-05",
                 "2026-12-01": "2026-12-01", "": None}
        for text, want in cases.items():
            self.assertEqual(parse_date(text, TODAY), want, text)
        with self.assertRaises(ValueError):
            parse_date("someday soon", TODAY)

    def test_recurrence(self):
        task = {"recur": "every week", "due": "2026-10-22", "scheduled": "2026-10-21", "start": None}
        self.assertEqual(next_dates(task, TODAY), {"scheduled": "2026-10-28", "due": "2026-10-29"})
        self.assertEqual(next_dates({"recur": "every month", "due": "2026-01-31"}, TODAY),
                         {"due": "2026-02-28"})
        self.assertEqual(next_dates({"recur": "every 2 days when done", "due": "2026-10-01"}, TODAY),
                         {"due": "2026-10-24"})
        with self.assertRaises(ValueError):
            next_dates({"recur": "every week on Monday", "due": "2026-10-22"}, TODAY)

    def test_task_requests_route_to_the_tool_brain(self):
        for text in ("add a task to buy paper", "remind me to call the bank friday",
                     "mark the drone sketch as done", "I finished assignment 2", "what's on my to-do list"):
            self.assertTrue(is_vault_request(text), text)
        self.assertFalse(is_vault_request("what's due this week?"))  # the brief answers that


class TaskCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name, "Vault")
        for rel, text in FILES.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text, encoding="utf-8", newline="")
        self.vault = Vault(self.root, Path(temp.name, "state"))
        self.tasks = Tasks(self.vault)

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")


class AgendaTests(TaskCase):
    def test_buckets(self):
        a = self.tasks.agenda(TODAY)
        titles = lambda name: [t["title"] for t in a[name]]
        self.assertEqual(titles("overdue"), ["Assignment 2 — due October 13, 2026"])
        self.assertEqual(sorted(titles("today")), ["Assignment 3", "Call the bank", "Lab 4",
                                                   "Start the cheat sheet"])
        self.assertEqual(titles("in_progress"), ["Sketch the frame"])  # started beats dated
        self.assertEqual(titles("upcoming"), [])
        self.assertEqual(titles("later"), ["Lab 5"])
        self.assertEqual(titles("anytime"), ["Decide what it is for"])
        self.assertEqual(titles("done"), ["Read about FPV"])
        self.assertEqual(a["checklists"], [{"path": "courses/plan.md", "note": "Study plan",
                                            "total": 20, "done": 3, "next": "Topic 3"}])
        everything = " ".join(t["text"] for t in self.tasks.all(TODAY))
        for absent in ("Teeth", "Old priority", "Assignment 1", "not a task"):
            self.assertNotIn(absent, everything)  # routine, stale daily, prose-dated, code
        open_titles = [t for k in ("overdue", "today", "upcoming", "later", "anytime") for t in titles(k)]
        self.assertNotIn("Week 4 (Due June 17th)", open_titles)  # a finished course's leftovers
        self.assertEqual(self.tasks.agenda(TODAY, query="lab")["counts"]["today"], 1)
        summary = " ".join(self.tasks.summary(TODAY))
        self.assertIn("Overdue tasks: Assignment 2", summary)
        self.assertIn("[Operating Systems]", summary)

    def test_lookup_by_id_or_words(self):
        task = self.tasks.get("sketch frame", TODAY)
        self.assertEqual(task["path"], "projects/drone.md")
        self.assertEqual(self.tasks.get(task["id"], TODAY)["line"], task["line"])
        with self.assertRaisesRegex(ValueError, "Several tasks match"):
            self.tasks.get("lab", TODAY)
        with self.assertRaisesRegex(ValueError, "No task matches"):
            self.tasks.get("zebra crossing", TODAY)


class WriteTests(TaskCase):
    def test_add_to_inbox_or_note_and_dedupe(self):
        made = self.tasks.add("Buy printer paper", "tomorrow", priority="high", when=NOW)
        self.assertEqual((made["path"], made["due"], made["priority"], made["existing"]),
                         ("tasks.md", "2026-10-23", "high", False))
        inbox = self.read("tasks.md")
        self.assertIn("```tasks\nnot done\ndue before today", inbox)  # the Obsidian dashboard
        self.assertIn("## Inbox\n\n- [ ] Buy printer paper ⏫ ➕ 2026-10-22 📅 2026-10-23\n", inbox)
        self.assertTrue(self.tasks.add("buy printer paper!", when=NOW)["existing"])
        self.tasks.add("Pick motors", "friday", note="drone", when=NOW)
        self.assertIn("- [x] Read about FPV ✅ 2026-10-20\n- [ ] Pick motors ➕ 2026-10-22 📅 2026-10-23\n"
                      "\n## Related", self.read("projects/drone.md"))
        self.tasks.add("Lab 6", note="os", when=NOW)  # no Tasks/Next step heading: one is made
        self.assertIn("## Tasks\n\n- [ ] Lab 6 ➕ 2026-10-22\n", self.read("courses/os.md"))
        for bad in ({"text": " "}, {"text": "x", "priority": "urgent"}, {"text": "x", "due": "soonish"},
                    {"text": "x", "note": "missing-note"}):
            with self.assertRaises(ValueError, msg=bad):
                self.tasks.add(**bad, when=NOW)

    def test_updates_edit_one_verified_line(self):
        self.tasks.update("decide what it is for", "done", when=NOW)
        self.tasks.update("sketch the frame", "due", "next week", when=NOW)
        self.tasks.update("assignment 2", "start", when=NOW)
        self.tasks.update("lab 5", "priority", "low", when=NOW)
        self.tasks.update("lab 4", "cancel", when=NOW)
        drone, os_ = self.read("projects/drone.md"), self.read("courses/os.md")
        self.assertIn("- [x] Decide what it is for ⏫ ✅ 2026-10-22\n", drone)
        self.assertIn("- [/] Sketch the frame 📅 2026-10-29\n", drone)
        self.assertIn("updated: 2026-10-22", drone)
        self.assertIn("- [/] Assignment 2 — due October 13, 2026\n", os_)
        self.assertIn("- [ ] Lab 5 🔽 📅 2026-11-30\n", os_)
        self.assertIn("- [-] Lab 4 ⏳ 2026-10-20 ❌ 2026-10-22\n", os_)
        self.tasks.update("decide what it is for", "reopen", when=NOW)
        self.assertIn("- [ ] Decide what it is for ⏫\n", self.read("projects/drone.md"))
        audit = [json.loads(l)["action"] for l in
                 (self.vault.state / "vault-writes.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(audit, ["task.done", "task.due", "task.start", "task.priority",
                                 "task.cancel", "task.reopen"])

    def test_recurring_done_adds_the_next_one_above(self):
        self.tasks.add("Water the plant", "today", recur="every week", when=NOW)
        self.tasks.update("water the plant", "done", when=NOW)
        self.assertIn("- [ ] Water the plant 🔁 every week ➕ 2026-10-22 📅 2026-10-29\n"
                      "- [x] Water the plant 🔁 every week ➕ 2026-10-22 📅 2026-10-22 ✅ 2026-10-22\n",
                      self.read("tasks.md"))

    def test_refuses_stale_lines_conflicts_and_read_only(self):
        task = self.tasks.get("sketch the frame", TODAY)
        path = self.root / "projects/drone.md"
        path.write_text(path.read_text("utf-8").replace("Sketch the frame", "Sketch the frame v2"),
                        "utf-8")
        self.tasks._cache.clear()
        with self.assertRaises(ValueError):  # its words no longer pick out the old task id
            self.tasks.update(task["id"], "done", when=NOW)
        path.write_text(path.read_text("utf-8") + "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n", "utf-8")
        with self.assertRaisesRegex(RuntimeError, "merge conflicts"):
            self.tasks.update("sketch the frame v2", "done", when=NOW)
        locked = Tasks(Vault(self.root, self.vault.state, {"write": False}))
        with self.assertRaisesRegex(ValueError, "writes are off"):
            locked.update("lab 5", "done", when=NOW)
        self.assertEqual([s["name"] for s in TaskTools(locked).specs()], ["task_list"])

    def test_tools(self):
        tools = TaskTools(self.tasks)
        ok, text = tools.call("task_add", {"text": "Email the TA", "due": "2026-10-24",
                                           "priority": "", "note": ""})
        self.assertTrue(ok, text)
        ok, text = tools.call("task_list", {"view": "agenda", "query": ""})
        agenda = json.loads(text)
        self.assertIn("Email the TA", [t["title"] for t in agenda["upcoming"]])
        self.assertEqual(agenda["checklists"][0]["done"], 3)
        ok, text = tools.call("task_update", {"task": "email the ta", "action": "done", "value": ""})
        self.assertTrue(ok, text)
        ok, text = tools.call("task_update", {"task": "email", "action": "explode", "value": ""})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
