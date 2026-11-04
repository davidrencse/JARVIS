"""Console feed watcher: lines written just before another process rolled the feed over
to events.1.jsonl are still relayed."""

import json
import tempfile
import unittest
from pathlib import Path

from jarvis.events import Bus, Watcher
from jarvis.memory import Memory
from jarvis.store import Store


def line(text):
    return json.dumps({"kind": "turn.sentence", "data": {"text": text}}) + "\n"


class RolloverTests(unittest.TestCase):
    def test_lines_added_before_rollover_are_not_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bus = Bus()
            watcher = Watcher(bus, base, Memory(base), Store(base))
            feed = base / "events.jsonl"
            feed.write_text(line("one"), encoding="utf-8")
            watcher._poll_feed()
            with open(feed, "a", encoding="utf-8") as f:
                f.write(line("two"))  # arrives, then the feed rolls before our next poll
            feed.replace(base / "events.1.jsonl")
            feed.write_text(line("three"), encoding="utf-8")
            watcher._poll_feed()
            self.assertEqual([e["data"]["text"] for e in bus.recent], ["one", "two", "three"])


if __name__ == "__main__":
    unittest.main()
