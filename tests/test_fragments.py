"""Phase 6 (2026-11-01): speech fragments are caught before the brain answers them.
Cases are real voice requests from the memory heap (2026-10-24..30) unless marked."""

import unittest

from jarvis.fragments import complete, judge

RAMBLE = ("is nice. Jarvis could guarantee me maybe draw, which is kind of what I want. When you "
          "made that? But the fintech guy, like, I don't know, I talk to him a lot. He's like, "
          "pretty true. Yeah. When you made that? Like it is what it is man, you know how it goes")


class JudgeTests(unittest.TestCase):
    def test_real_fragments(self):
        self.assertEqual(judge("Alel.")[0], "unclear")
        self.assertEqual(judge("for the day.")[0], "unclear")
        self.assertEqual(judge("open up"), ("incomplete", "Open what?"))
        self.assertEqual(judge("go to my Arch Linux and open up"), ("incomplete", "Open what?"))
        self.assertEqual(judge("close"), ("incomplete", "Close what?"))
        self.assertEqual(judge(RAMBLE, follow_up=True)[0], "overheard")

    def test_real_requests_pass(self):
        for text in ("hello", "ask Brockbot.", "open up Google.", "open up Spotify.",
                     "what's today's date?", "what time is it?", "connect to my Arch Linux.",
                     "say hello to my friends", "I'm going to sell my car.",
                     "what apps are on my Windows laptop?", "how much is Bitcoin right now?",
                     "can you open up Google Chrome?", "turn the volume up", "next", "thanks",
                     "speak up", "switch the lights on",
                     "open up Google Chrome on my Arch Linux and open up Spotify on my Dell laptop.",
                     "what do I say to my girl to calm her down?"):
            self.assertEqual(judge(text, follow_up=True)[0], "keep", text)

    def test_the_ramble_counts_only_without_a_wake_word(self):
        self.assertEqual(judge(RAMBLE)[0], "keep")  # "Jarvis, ..." said on purpose: answered

    def test_long_dictated_requests_pass(self):  # not from the heap: the shape of one
        text = ("what is the best way to learn rust? I have tried books. Nothing works. Any tips "
                "for someone who learns by building projects and gets bored with exercises fast?")
        self.assertEqual(judge(text, follow_up=True)[0], "keep")

    def test_what_the_parser_understands_always_passes(self):
        self.assertEqual(judge("spotify", understood=lambda t: True)[0], "keep")
        self.assertEqual(judge("spotify", understood=lambda t: False)[0], "unclear")
        self.assertEqual(judge("spotify", understood=lambda t: 1 / 0)[0], "unclear")  # never raises

    def test_halves_join(self):
        self.assertEqual(complete("open up", "Spotify."), "open up Spotify.")
        self.assertEqual(complete("go to my Arch Linux and open up.", "Chrome"),
                         "go to my Arch Linux and open up Chrome")


if __name__ == "__main__":
    unittest.main()
