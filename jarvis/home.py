"""Where the JARVIS checkout lives (config, .jarvis state, JARVIS.md, tests, .venv).

Normally that is the folder above the ``jarvis`` package. When the main runs from the tested
release copy (``cluster.run_from_release``: the package sits in ``<state>/code-release/jarvis``),
that folder is a code snapshot with no config, state or venv, so the unit sets ``JARVIS_HOME`` to
the real checkout. The variable is honoured only from inside a ``code-release`` folder: a test
stage or an ordinary checkout that inherits it still resolves to itself.
"""
import os
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parent.parent


def in_release():
    return PACKAGE_PARENT.name == "code-release"


def home():
    """The checkout this process works for."""
    env = os.environ.get("JARVIS_HOME")
    if env and in_release():
        folder = Path(env)
        if folder.is_absolute() and folder.is_dir():
            return folder.resolve()
    return PACKAGE_PARENT
