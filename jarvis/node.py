"""Lightweight installed entry point: jarvis-node [--config FILE] [--state DIR]."""

import sys


def main():
    from .__main__ import main as cli
    sys.argv = [sys.argv[0], *sys.argv[1:], "cluster", "node"]
    return cli()


if __name__ == "__main__":
    raise SystemExit(main())
