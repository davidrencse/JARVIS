"""Knowledge engine: reader keeps raw/ untouched, librarian files claims with sources and
dates, checker doubts. Everything here is offline and deterministic."""

import hashlib
import json
import os
import re
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from jarvis.knowledge import (KnowledgeBase, claim_line, extract_claims, parse_page, recall,
                              relation, render_page, stem, topics)

ROUTINES = ("# Routines\n\nThe review routine runs daily at 02:00 and checks every page.\n\n"
            "The review routine sends a summary to David by email.\n\n"
            "Backups of the vault run nightly to the NAS drive.\n\n"
            "The NAS drive keeps 30 days of vault backups.")
WEEKLY = ("# Weekly ops\n\nThe review routine runs weekly on Sunday mornings.\n\n"
          "The NAS drive keeps 30 days of vault backups.\n\n"
          "David reads the weekly review summary on Monday.")


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "knowledge")
        self.kb = KnowledgeBase(self.root, today=date(2026, 9, 1))

    def tearDown(self):
        for path in self.root.rglob("*"):  # raw/ is read-only on purpose
            if path.is_file():
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        self.tmp.cleanup()


class AlgorithmTests(unittest.TestCase):
    def test_stems_meet(self):
        self.assertEqual(stem("routines"), stem("routine"))
        self.assertEqual(stem("runs"), stem("running"))
        self.assertEqual(stem("scheduled"), stem("schedule"))

    def test_contradictions_by_facet(self):
        cases = {
            ("The review routine runs daily at 02:00.", "The review routine runs weekly."): "conflict",
            ("The Dell laptop has 16 GB of memory.", "The Dell laptop has 32 GB of memory."): "conflict",
            ("Vault writes are enabled by default.", "Vault writes are disabled by default."): "conflict",
            ("The listener uses the CPU model.", "The listener does not use the CPU model."): "conflict",
            ("Lan Mouse uses port 4242 on the Zenbook.", "Lan Mouse uses port 4243 on the Zenbook."): "conflict",
            ("The backup takes 90 seconds on the NAS.", "The backup takes 1.5 minutes on the NAS."): "duplicate",
            ("The backup takes 90 seconds on the NAS.", "The backup takes 5 minutes on the NAS."): "conflict",
            ("The review routine runs daily.", "The review routine runs every day."): "duplicate",
            ("Kokoro runs on the CPU.", "Whisper runs on the GPU."): None,
            ("The Dell laptop has 16 GB of memory.", "The Zenbook has 32 GB of memory."): None,
        }
        for (a, b), want in cases.items():
            got = relation(a, b)
            self.assertEqual(got[0] if got else None, want, (a, b, got))

    def test_claims_skip_questions_headings_code_and_boilerplate(self):
        text = ("---\ntitle: x\n---\n# Heading only\n\nIs this a claim?\n\n```\ncode runs daily\n```\n\n"
                "Click here to subscribe to the newsletter today.\n\n"
                "- The listener runs on the CPU when no GPU is present.\n"
                "| table | row |\n\nJARVIS keeps every raw source read-only after ingest.")
        claims = extract_claims(text)
        self.assertEqual(claims, ["The listener runs on the CPU when no GPU is present.",
                                  "JARVIS keeps every raw source read-only after ingest."])

    def test_topics_are_named_after_things(self):
        claims = ["The vault holds every note David writes.", "The vault syncs through obsidian-git.",
                  "The vault refuses writes to calendar.md.", "Kokoro speaks every reply.",
                  "Kokoro runs on the CPU.", "Kokoro voices live in the state folder."]
        names = {name for name, _ in topics(claims)}
        self.assertEqual(names, {"vault", "kokoro"})

    def test_page_round_trip_keeps_hand_written_lines(self):
        page = {"slug": "x", "title": "X", "type": "page", "question": "", "answer": "",
                "related": ["y"], "extra": "## Notes\nWritten by David.",
                "claims": [{"id": "c123abc", "text": "A claim with: colons — and dashes.",
                            "date": "2026-10-01", "sources": ["source-001", "source-004"],
                            "flag": "conflict", "with": ["c999999"]}]}
        text = render_page(page, date(2026, 10, 2))
        again = parse_page("x", text)
        self.assertEqual(again["claims"], page["claims"])
        self.assertEqual(again["related"], ["y"])
        self.assertEqual(again["notes"], ["Written by David."])
        self.assertIn("status: review", text)
        self.assertEqual(render_page(again, date(2026, 10, 2)), text)
        self.assertIn("source: [[source-001]], [[source-004]] · confirmed: 2026-10-01",
                      claim_line(page["claims"][0]))


class HandEditTests(unittest.TestCase):
    def test_what_people_add_survives_a_rewrite(self):
        text = ("---\ntitle: \"Draft\ntype: page\ntags: [ops, nas]\n---\n# Draft\n\n## Claims\n"
                "- Fine claim here today. ^c111111 — source: [[source-001]] · confirmed: 2026-10-01\n"
                "- Bad date claim. ^c222222 — source: [[source-001]] · confirmed: 2026-02-30\n\n"
                "## Related\n[[nas]] · check with Priya first\n")
        page = parse_page("draft", text)
        self.assertEqual([c["id"] for c in page["claims"]], ["c111111"])
        out = render_page(page, date(2026, 10, 2))
        self.assertIn("tags: [ops, nas]", out)
        self.assertIn("check with Priya first", out)
        self.assertIn("confirmed: 2026-02-30", out)  # kept as a note, not lost
        self.assertEqual(render_page(parse_page("draft", out), date(2026, 10, 2)), out)


class ReaderTests(Temp):
    def test_json_sources_and_a_lost_engine_file(self):
        data = Path(self.tmp.name, "facts.json")
        data.write_text('{"sha256": "nope", "note": "The NAS drive keeps 30 days of vault backups."}')
        meta = self.kb.ingest(data)["source"]
        self.assertEqual(self.kb.source(meta["id"])["sha256"], meta["sha256"])
        self.assertEqual(len(self.kb.sources()), 1)
        self.kb.ingest(text=ROUTINES)
        self.kb.ingest(text=WEEKLY)
        self.assertEqual(self.kb.ask("how often does the review routine run")["filed"], "answer-001")
        os.remove(self.root / "engine.json")
        self.assertEqual(self.kb.ingest(text="The vault syncs every hour through obsidian-git.")["source"]["id"], "source-004")
        self.assertEqual(self.kb.ask("does the NAS drive keep vault backups for 30 days")["filed"], "answer-002")
        self.assertIn("how often", self.kb.page("answer-001")["question"])

    def test_failed_extraction_leaves_nothing_behind(self):
        audio = Path(self.tmp.name, "memo.wav")
        audio.write_bytes(b"RIFF....")
        def broken(path):
            raise RuntimeError("decoder failed")
        with self.assertRaises(RuntimeError):
            self.kb.ingest(audio, transcribe=broken)
        self.assertEqual(list((self.root / "raw").iterdir()), [])
        self.assertEqual(self.kb.ingest(audio, transcribe=lambda p: "A memo about the vault backups schedule.")["source"]["id"], "source-001")

    def test_a_topic_called_index_does_not_eat_the_index(self):
        self.kb.ingest(text="The index rebuilds after every write.\n\nThe index lists each page with its status.\n\n"
                            "The index skips archived pages entirely.", title="Index")
        self.assertIn("index-page", self.kb.pages())
        self.assertIn("# Index", (self.root / "wiki" / "index.md").read_text())
        with self.assertRaises(ValueError):
            self.kb.merge("index-page", "index")

    def test_raw_is_read_only_hashed_and_deduplicated(self):
        first = self.kb.ingest(text=ROUTINES)
        meta = first["source"]
        raw = self.root / "raw" / meta["file"]
        self.assertFalse(os.access(raw, os.W_OK))
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), meta["sha256"])
        again = self.kb.ingest(text=ROUTINES)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.kb.sources()), 1)

    def test_files_html_and_links(self):
        page = Path(self.tmp.name, "page.html")
        page.write_text("<html><title>T</title><body><p>The vault refuses writes to the "
                        "calendar file by design.</p><script>x()</script></body></html>")
        result = self.kb.ingest(page)
        self.assertEqual(result["source"]["kind"], "page")
        self.assertIn("calendar file", self.kb.source_text(result["source"]["id"]))
        fetched = self.kb.ingest("https://example.com/a", fetch=lambda url: {
            "url": url, "title": "Example", "text": "Example pages document the review routine well."})
        self.assertEqual(fetched["source"]["origin"], "https://example.com/a")
        with self.assertRaises(ValueError):
            self.kb.ingest(Path(self.tmp.name, "missing.md"))


class LibrarianTests(Temp):
    def test_claims_carry_source_and_date_and_repeats_reconfirm(self):
        self.kb.ingest(text=ROUTINES)
        self.kb._today = date(2026, 10, 17)
        result = self.kb.ingest(text=WEEKLY)["librarian"]
        self.assertEqual(result["reconfirmed"], 1)  # the NAS line
        nas = next(c for p in self.kb.pages().values() for c in p["claims"] if "NAS drive keeps" in c["text"])
        self.assertEqual(nas["sources"], ["source-001", "source-002"])
        self.assertEqual(nas["date"], "2026-10-17")

    def test_contradiction_keeps_both_and_flags_each(self):
        self.kb.ingest(text=ROUTINES)
        result = self.kb.ingest(text=WEEKLY)["librarian"]
        self.assertEqual(result["conflicts"], 1)
        claims = {c["text"]: c for p in self.kb.pages().values() for c in p["claims"]}
        daily = claims["The review routine runs daily at 02:00 and checks every page."]
        weekly = claims["The review routine runs weekly on Sunday mornings."]
        self.assertEqual((daily["flag"], weekly["flag"]), ("conflict", "conflict"))
        self.assertEqual(daily["with"], [weekly["id"]])
        self.assertEqual(weekly["with"], [daily["id"]])
        self.assertTrue(any(e["action"] == "conflict" for e in self.kb.log()))

    def test_every_write_is_atomic_and_logged_with_a_diff(self):
        self.kb.ingest(text=ROUTINES)
        events = self.kb.log()
        creates = [e for e in events if e["action"] == "create"]
        self.assertTrue(creates)
        self.assertTrue(all(e["diff"][0].startswith("--- a/wiki/") for e in creates))
        commit = next(e for e in events if e["action"] == "commit")
        self.assertEqual(sorted(commit["writes"]), sorted(e["target"][5:-3] for e in creates))
        self.assertFalse(list((self.root / "wiki").glob("*.tmp")))
        self.assertIn("[[", (self.root / "wiki" / "index.md").read_text())


class AnswerTests(Temp):
    def test_good_answers_are_filed_back_and_refreshed(self):
        self.kb.ingest(text=ROUTINES)
        self.kb.ingest(text=WEEKLY)
        first = self.kb.ask("How often does the review routine run?")
        self.assertEqual(first["filed"], "answer-001")
        self.assertIn("disputed", first["answer"])
        self.assertIn("[source-001", first["answer"])
        again = self.kb.ask("how often does the review routine run")
        self.assertEqual(again["filed"], "answer-001")
        page = self.kb.page("answer-001")
        self.assertEqual(page["type"], "answer")
        self.assertTrue(page["related"])

    def test_unknown_questions_are_not_filed(self):
        self.kb.ingest(text=ROUTINES)
        result = self.kb.ask("what is the capital of peru")
        self.assertIsNone(result["filed"])
        self.assertEqual(result["claims"], [])

    def test_llm_answer_must_cite_only_given_claims(self):
        self.kb.ingest(text=ROUTINES)
        hits = self.kb.retrieve("review routine")
        good = f"It runs daily [{hits[0]['id']}]."
        with patch("jarvis.knowledge.shutil.which", return_value="/usr/bin/claude"), \
                patch("jarvis.knowledge.subprocess.run") as run:
            run.return_value.stdout = good
            self.assertEqual(self.kb._llm_answer("q", hits), good)
            run.return_value.stdout = "It runs hourly [cdeadbe]."
            self.assertIsNone(self.kb._llm_answer("q", hits))
            run.return_value.stdout = "No citations at all."
            self.assertIsNone(self.kb._llm_answer("q", hits))


class CheckerTests(Temp):
    def test_stale_claims_become_unconfirmed_and_fresh_sources_clear_it(self):
        self.kb.ingest(text=ROUTINES)
        self.kb._today = date(2026, 10, 15)
        report = self.kb.check()
        self.assertEqual(len(report["stale"]), 4)
        self.assertIn("status: unconfirmed", (self.root / "wiki" / "nas-drive.md").read_text())
        self.kb.ingest(text="The NAS drive keeps 30 days of vault backups.\n\nAnother line here for the NAS drive.")
        nas = next(c for p in self.kb.pages().values() for c in p["claims"] if "keeps 30 days" in c["text"])
        self.assertIsNone(nas["flag"])

    def test_cross_page_contradiction_and_raw_mutation(self):
        self.kb.ingest(text=ROUTINES)
        meta = self.kb.ingest(text=WEEKLY)["source"]
        # Someone hand-edits a page so a conflict is no longer marked; the checker re-finds it.
        for path in (self.root / "wiki").glob("*.md"):
            path.write_text(re.sub(r"\[!conflict [^\]]*\] ", "", path.read_text()))
        raw = self.root / "raw" / meta["file"]
        os.chmod(raw, stat.S_IWRITE | stat.S_IREAD)
        raw.write_text("tampered")
        report = self.kb.check()
        self.assertEqual(report["mutated"], [meta["id"]])
        self.assertEqual(len(report["new_conflicts"]), 1)
        flags = [e for e in self.kb.log() if e["action"] == "flag"]
        self.assertTrue(any("@you" in e["message"] for e in flags))

    def test_report_is_saved_and_orphans_listed(self):
        self.kb.ingest(text=ROUTINES)
        report = self.kb.check()
        saved = json.loads((self.root / "checks" / "2026-09-01.json").read_text())
        self.assertEqual(saved["scanned"], report["scanned"])
        self.assertIsInstance(report["orphans"], list)
        self.assertEqual(self.kb.snapshot()["checker"]["report"]["scanned"], report["scanned"])

    @unittest.skipIf(os.name == "nt", "systemd timer")
    def test_schedule_writes_a_user_timer(self):
        with tempfile.TemporaryDirectory() as cfg, patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}), \
                patch("jarvis.knowledge.subprocess.run") as run:
            run.return_value.returncode = 0
            made = self.kb.schedule(True, "jarvis.config.json", self.tmp.name, "03:30")
            timer = Path(made["timer"]).read_text()
            self.assertIn("OnCalendar=*-*-* 03:30:00", timer)
            service = Path(made["timer"]).with_suffix(".service").read_text()
            self.assertIn('"kb" "check" "--notify"', service)
            self.assertTrue(self.kb.schedule_status()["installed"])
            self.kb.schedule(False, "jarvis.config.json", self.tmp.name)
            self.assertFalse(Path(made["timer"]).exists())


class RulingTests(Temp):
    def setUp(self):
        super().setUp()
        self.kb.ingest(text=ROUTINES)
        self.kb.ingest(text=WEEKLY)
        conflict = self.kb.open_conflicts(self.kb.pages())[0]
        self.daily, self.weekly = sorted((conflict["claim"], conflict["rival"]),
                                         key=lambda c: "daily" not in self.claim(c)["text"])

    def claim(self, cid):
        return next(c for p in self.kb.pages().values() if p["type"] == "page"
                    for c in p["claims"] if c["id"] == cid)

    def test_keep_supersedes_the_rival_and_the_checker_remembers(self):
        self.kb.ask("how often does the review routine run")  # a copy in an answer page too
        self.kb.resolve(self.weekly, "keep")
        self.assertIsNone(self.claim(self.weekly)["flag"])
        self.assertEqual(self.claim(self.daily)["flag"], "superseded")
        self.assertEqual(self.claim(self.daily)["with"], [self.weekly])
        answer = self.kb.page("answer-001")
        self.assertTrue(all(c["flag"] != "conflict" for c in answer["claims"]))
        self.assertEqual(self.kb.open_conflicts(self.kb.pages()), [])
        self.assertEqual(self.kb.check()["new_conflicts"], [])
        hits = self.kb.retrieve("how often does the review routine run")
        self.assertNotIn(self.daily, [h["id"] for h in hits])  # history, not evidence
        self.assertIn("superseded", (self.root / "wiki" / "review-routine.md").read_text())

    def test_both_true_clears_flags_for_good(self):
        result = self.kb.resolve(self.daily, "both")
        self.assertEqual(result["rivals"], [self.weekly])
        self.assertIsNone(self.claim(self.daily)["flag"])
        self.assertIsNone(self.claim(self.weekly)["flag"])
        self.assertEqual(self.kb.check()["new_conflicts"], [])
        self.assertIn("|".join(sorted((self.daily, self.weekly))), self.kb.bot("checker")["decisions"])
        self.kb.ingest(text="The review routine runs daily at 02:00 and checks every page.\n\nSome other line about the review routine schedule.")
        self.assertIsNone(self.claim(self.daily)["flag"])

    def test_repeat_reconfirms_the_page_not_an_answer_copy(self):
        self.kb.ask("how often does the review routine run")
        self.kb._today = date(2026, 10, 20)
        self.kb.ingest(text="The review routine sends a summary to David by email.\n\nA line about mail.")
        self.assertEqual(self.claim(next(c["id"] for p in self.kb.pages().values() for c in p["claims"]
                                         if "summary to David" in c["text"]))["date"], "2026-10-20")

    def test_drop_and_bad_rulings(self):
        self.kb.resolve(self.daily, "drop")
        self.assertEqual(self.claim(self.daily)["flag"], "superseded")
        with self.assertRaises(ValueError):
            self.kb.resolve(self.weekly, "keep")  # nothing left to rule on
        with self.assertRaises(LookupError):
            self.kb.resolve("c000000", "keep")
        with self.assertRaises(ValueError):
            self.kb.resolve(self.weekly, "maybe")


class ThreeWayTests(Temp):
    def test_a_ruling_on_one_pair_leaves_the_others_open(self):
        self.kb.ingest(text="The review routine runs daily on the server.\n\nFiller sentence about the server room here.")
        self.kb.ingest(text="The review routine runs weekly on the server.\n\nAnother filler line about server racks.")
        self.kb.ingest(text="The review routine runs monthly on the server.\n\nYet another line on server cooling.")
        claims = {c["text"].split()[4]: c["id"] for p in self.kb.pages().values() for c in p["claims"]
                  if c["text"].startswith("The review routine runs")}
        self.assertEqual(len(self.kb.open_conflicts(self.kb.pages())), 3)
        self.kb.resolve(claims["daily"], "keep", against=claims["weekly"])
        open_pairs = {tuple(sorted((c["claim"], c["rival"]))) for c in self.kb.open_conflicts(self.kb.pages())}
        self.assertEqual(open_pairs, {tuple(sorted((claims["daily"], claims["monthly"])))})
        with self.assertRaises(ValueError):
            self.kb.resolve(claims["daily"], "keep", against=claims["weekly"])


class VaultSyncTests(Temp):
    def test_same_filename_in_different_folders_gets_separate_pages(self):
        from jarvis.vault import Vault
        root = Path(self.tmp.name, "vault2")
        for folder, name in (("courses", "Courses"), ("projects", "Projects")):
            (root / folder).mkdir(parents=True)
            (root / folder / "README.md").write_text(
                f"---\ntitle: {name} readme\n---\n# {name}\n\n{name} notes live in this folder.\n")
        vault = Vault(root, Path(self.tmp.name, "vstate2"))
        self.kb.sync_vault(vault)
        pages = self.kb.pages()
        self.assertEqual({pages[s]["title"] for s in ("courses-readme", "projects-readme")},
                         {"Courses readme", "Projects readme"})

    def test_notes_become_one_page_each_and_only_changes_are_reread(self):
        from jarvis.vault import Vault
        root = Path(self.tmp.name, "vault")
        (root / "notes").mkdir(parents=True)
        (root / "daily").mkdir()
        note = root / "notes" / "lan-mouse.md"
        note.write_text("---\ntitle: Lan Mouse\ntype: note\n---\n# Lan Mouse\n\n"
                        "Lan Mouse shares one keyboard between both laptops.\n\n"
                        "Lan Mouse listens on UDP port 4242 on both laptops.\n")
        (root / "daily" / "2026-10-22.md").write_text("Lan Mouse was slow today on the train wifi.")
        vault = Vault(root, Path(self.tmp.name, "vstate"))
        first = self.kb.sync_vault(vault)
        self.assertEqual(first["added"], ["notes/lan-mouse.md"])  # daily notes stay out
        page = self.kb.pages()["lan-mouse"]
        self.assertEqual(page["title"], "Lan Mouse")
        self.assertEqual(len(page["claims"]), 2)
        self.assertEqual(self.kb.sync_vault(vault)["unchanged"], 1)
        note.write_text(note.read_text() + "\nLan Mouse needs the firewall open on the Dell side.\n")
        self.assertEqual(self.kb.sync_vault(vault)["added"], ["notes/lan-mouse.md"])
        self.assertEqual(len(self.kb.pages()["lan-mouse"]["claims"]), 3)

    def test_one_source_contrasting_two_cases_is_not_a_conflict(self):
        result = self.kb.ingest(text="In the study their body reacts to the blue square.\n\n"
                                     "In the control group their body does not react to the blue square.")
        self.assertEqual(result["librarian"]["conflicts"], 0)
        self.assertEqual(self.kb.check()["new_conflicts"], [])
        other = self.kb.ingest(text="Their body does not react to the blue square in the study.\n\nFiller.")
        self.assertGreaterEqual(other["librarian"]["conflicts"], 1)  # another source disagreeing is


class MergeTests(Temp):
    def test_merge_moves_claims_and_future_claims_follow(self):
        self.kb.ingest(text=ROUTINES)
        before = len(self.kb.pages()["nas-drive"]["claims"])
        result = self.kb.merge("nas-drive", "Backups")
        self.assertEqual(result["into"], "backups")
        pages = self.kb.pages()
        self.assertNotIn("nas-drive", pages)
        self.assertEqual(len(pages["backups"]["claims"]), before)
        self.assertEqual(self.kb.bot("librarian")["aliases"], {"nas-drive": "backups"})
        self.kb.ingest(text="The NAS drive mirrors photos weekly.\n\nThe NAS drive sleeps after an hour of idle time.")
        self.assertNotIn("nas-drive", self.kb.pages())
        with self.assertRaises(LookupError):
            self.kb.merge("nas-drive", "x")


class IntegrationTests(Temp):
    def test_recall_lines_and_never_raises(self):
        state = Path(self.tmp.name)
        self.kb.ingest(text=ROUTINES)
        lines = recall({}, state, "when does the review routine run")
        self.assertTrue(lines and lines[0].startswith("[wiki "))
        self.assertIn("source-001", lines[0])
        self.assertEqual(recall({"knowledge": {"path": "/nonexistent/x"}}, state, "x"), [])

    def test_console_commands(self):
        from jarvis.ui import Console
        console = Console({"knowledge": {"path": str(self.root)}}, ".", self.tmp.name)
        with self.assertRaises(ValueError):
            console.kb_command("reader ingest /etc/passwd")  # files are CLI-only
        with self.assertRaises(ValueError):
            console.kb_command("   ")
        with self.assertRaises(ValueError):  # never quietly becomes a question
            console.kb_command("resolve c1a2b3c keep because it is newer")
        with self.assertRaises(ValueError):
            console.kb_command("merge just-one")
        seen = []
        console.bus.publish = lambda kind, data=None: seen.append(kind)
        with patch("jarvis.ui.threading.Thread") as thread:
            thread.side_effect = lambda target, daemon: type("T", (), {"start": staticmethod(target)})
            console.kb_command("> note The review routine runs daily at 02:00 and checks every page.")
            console.kb_command("checker audit")
        self.assertIn("kb.ingest", seen)
        import io
        with patch("jarvis.ui.threading.Thread") as thread:
            thread.side_effect = lambda target, daemon: type("T", (), {"start": staticmethod(target)})
            body = b"The NAS drive keeps 30 days of vault backups on the shelf."
            self.assertEqual(console.kb_upload(io.BytesIO(body), len(body), "../../notes.md")["accepted"], "notes.md")
            with self.assertRaises(ValueError):
                console.kb_upload(io.BytesIO(b"x"), 1, "run.exe")
            with self.assertRaises(ValueError):
                console.kb_upload(io.BytesIO(b"short"), 50, "cut.md")
        self.assertFalse(list((self.root / "inbox").iterdir()))  # nothing left behind
        self.assertEqual(console.kb().snapshot()["stats"]["sources"], 2)
        self.assertIn("kb.audit", seen)



class ClaimIdTests(Temp):
    def test_new_ids_are_12_hex_and_old_ids_still_parse(self):
        from jarvis.knowledge import claim_id, CLAIM_LINE
        self.assertRegex(claim_id("The review routine runs daily."), r"^c[0-9a-f]{12}$")
        for cid in ("c1a2b3c", "c1a2b3c4d5e6f"):
            line = f"- Some text. ^{cid} \u2014 source: source-001 \u00b7 confirmed: 2026-09-01"
            self.assertEqual(CLAIM_LINE.match(line)["id"], cid)

    def test_colliding_id_of_a_different_claim_is_not_reconfirmed(self):
        from jarvis.knowledge import claim_id
        real = claim_id
        first = "The review routine runs daily at 02:00."
        second = "The vault backup keeps thirty copies on the NAS."
        with patch("jarvis.knowledge.claim_id", side_effect=lambda t, size=12: real(first, size)):
            self.kb.ingest(text=first)
            self.kb.ingest(text=second)
        texts = [c["text"] for p in self.kb.pages().values() for c in p["claims"]]
        self.assertEqual(len(texts), 2)

    def test_legacy_six_digit_claim_is_recognised_as_same(self):
        from jarvis.knowledge import claim_id
        self.kb.ingest(text=ROUTINES)
        pages = self.kb.pages()
        before = sum(len(p["claims"]) for p in pages.values())
        slug, page = next((s, p) for s, p in pages.items() if p["claims"])
        for c in page["claims"]:
            c["id"] = claim_id(c["text"], 6)
        self.kb._save(pages, {}, "librarian", "legacy ids")
        self.kb.ingest(text=ROUTINES + "\n\nOne more line about the router.")
        after = sum(len(p["claims"]) for p in self.kb.pages().values())
        self.assertLessEqual(after, before + 1)


class RelinkOnceTests(Temp):
    def test_sync_relinks_once_and_single_ingest_relinks_each_time(self):
        calls = []
        real = KnowledgeBase._relink
        with patch.object(KnowledgeBase, "_relink", lambda self, pages: (calls.append(1), real(self, pages))[1]):
            self.kb.ingest(text=ROUTINES)
            self.assertEqual(len(calls), 1)
            self.kb.ingest(text=WEEKLY)
            self.assertEqual(len(calls), 2)
            self.kb.ingest(text="# Extra\n\nThe router restarts at night.", relink=False)
            self.assertEqual(len(calls), 2)


class SyncRelinkTests(Temp):
    def test_vault_sync_relinks_once(self):
        from jarvis.vault import Vault
        root = Path(self.tmp.name, "vault3")
        root.mkdir()
        for i in range(3):
            (root / f"note{i}.md").write_text(f"---\ntitle: Note {i}\n---\n# Note {i}\n\n"
                                              f"Note {i} describes the router number {i}.\n")
        calls = []
        real = KnowledgeBase._relink
        with patch.object(KnowledgeBase, "_relink", lambda self, pages: (calls.append(1), real(self, pages))[1]):
            result = self.kb.sync_vault(Vault(root, Path(self.tmp.name, "vstate3")))
        self.assertEqual(len(result["added"]), 3)
        self.assertEqual(len(calls), 1)


class RetrieveCacheTests(Temp):
    def test_cached_retrieve_matches_uncached_and_follows_changes(self):
        self.kb.ingest(text=ROUTINES)
        self.kb.ingest(text=WEEKLY)
        for q in ("review routine", "NAS drive backups", "weekly summary Monday", "nothing relevant zebra"):
            cached = self.kb.retrieve(q)
            self.assertEqual(cached, self.kb.retrieve(q, pages=self.kb.pages()))
            self.assertEqual(cached, self.kb.retrieve(q))  # second call hits the cache
        self.kb.ingest(text="# Zebras\n\nThe zebra enclosure opens at nine.")
        self.assertTrue(self.kb.retrieve("zebra enclosure"))


if __name__ == "__main__":
    unittest.main()
