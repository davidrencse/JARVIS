# Moving JARVIS's main to another machine (e.g. the Arch Zenbook)

1. **On the current main: commit everything.** (Historical walkthrough: the Dell was the old main; the move to the Zenbook is done, `cluster.main_device` is `zenbook`.) `git status` must be clean, then push.
   `.jarvis/` is not in git — copy it separately (step 4).
2. **On the new machine: get a clean checkout.** The Zenbook's `~/projects/JARVIS` was
   tar-synced by the cluster deployer, so it isn't a clean git tree. Either
   `mv ~/projects/JARVIS ~/projects/JARVIS.old && git clone <remote> ~/projects/JARVIS`, or
   inside it `git fetch && git reset --hard origin/main` (a backup `.tgz` already exists).
3. **Python deps:** create a venv and `pip install -e .` (pyproject.toml); JARVIS never downloads at run time, so fetch the MiniLM ONNX model once: `python -c "from huggingface_hub import hf_hub_download as d; M='sentence-transformers/all-MiniLM-L6-v2'; d(M,'onnx/model.onnx'); d(M,'tokenizer.json')"`. Without it the decider and semantic memory silently stay off.
4. **State for continuity:** done on 2026-10-26. Consistent snapshots (SQLite backup API,
   safe while running) of the Dell's `memory.sqlite3` and `history.sqlite3` are on the
   Zenbook in `~/projects/JARVIS/.jarvis/from-dell/`. When the new machine becomes main, stop
   JARVIS there and move them into `.jarvis/` (keep its old files as `*.pre-main`).
   (As of 2026-10-29 the Zenbook's `.jarvis/` has no `from-dell/` folder left, only its own
   `memory.sqlite3`/`history.sqlite3`.)
5. **Make the Zenbook main:** on the Zenbook `python -m jarvis cluster init` (forward tunnel, hub at
   login; it says if sshd is off), then `python -m jarvis cluster invite dell`.
6. **Join the Dell:** on the Dell, in its checkout, `python -m jarvis cluster join "<code>"`. It starts
   the tunnel and node agent and installs both at login; the node pulls the main's code itself.
   `python -m jarvis cluster doctor` on either machine shows what is left.
7. **Vault:** currently off (`vault.enabled`, `recall`, `write` all false). If turned on, `vault.path.linux` points at `~/Documents/obsidian-vault`; make sure it syncs there.
8. **Claude context:** this repo's `CLAUDE.md`, `JARVIS.md` and `docs/` travel with git. The memory notes are also
   in `jarvis-memory/` (committed) and were copied to `~/.claude/projects/-home-<user>-projects-JARVIS/memory/`.
9. **Check:** `python -m jarvis selftest keyboard` (Windows only; it skips elsewhere) and `python -m jarvis cluster status`.
