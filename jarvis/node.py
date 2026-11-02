"""Lightweight installed entry point: jarvis-node [--config FILE] [--state DIR]."""

import sys


def main():
    from .__main__ import main as cli
    args = sys.argv[1:]
    if args[-2:] != ["cluster", "node"]:  # a self-restart relaunches with it already there
        args = [*args, "cluster", "node"]
    sys.argv = [sys.argv[0], *args]
    return cli()


if __name__ == "__main__":
    raise SystemExit(main())
