"""JARVIS test suite. Run it all with: python -m unittest discover -s tests -t ."""

import os

os.environ.setdefault("JARVIS_NO_NOTIFY", "1")  # no desktop popups while testing
