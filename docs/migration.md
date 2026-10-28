# Moving JARVIS's main to another machine (e.g. the Arch Zenbook)

1. **On the current main (Dell): commit everything.** `git status` must be clean, then push.
   `.jarvis/` is not in git — copy it separately (step 4).
2. **On the new machine: get a clean checkout.** The Zenbook's `~/projects/JARVIS` was
   tar-synced by the cluster deployer, so it isn't a clean git tree. Either
   `mv ~/projects/JARVIS ~/projects/JARVIS.old && git clone <remote> ~/projects/JARVIS`, or
   inside it `git fetch && git reset --hard origin/main` (a backup `.tgz` already exists).
3. **Python deps:** create a venv and `pip install -e .` (pyproject.toml); the MiniLM ONNX model downloads on first semantic use.
4. **State for continuity:** done on 2026-10-26. Consistent snapshots (SQLite backup API,
   safe while running) of the Dell's `memory.sqlite3` and `history.sqlite3` are on the
   Zenbook in `~/projects/JARVIS/.jarvis/from-dell/`. When the Zenbook becomes main, stop
   JARVIS there and move them into `.jarvis/` (keep the Zenbook's old files as `*.pre-main`).
5. **Make the Zenbook main:** `python -m jarvis cluster main zenbook` (check with `cluster roles`), and set
   `"cluster": {"tunnel": "forward"}` so the Dell (node) dials the Zenbook's sshd — Windows needs
   no SSH server then. Give `devices.zenbook` `transport: ssh` and a `target` the Dell can reach.
6. **Autostart:** `python -m jarvis cluster autostart on` on each machine (systemd user unit on
   Arch, Startup .vbs on Windows). Then `cluster update dell` to push matching code to the node (SSH nodes), or just leave a
   forward-mode node (no sshd, e.g. the Dell) running: it pulls and verifies the main's code over its own
   authenticated hub channel on its next poll and restarts itself.
7. **Vault:** `vault.path.linux` in config points at `~/Documents/obsidian-vault`; make sure it syncs there.
8. **Claude context:** this repo's `CLAUDE.md`, `JARVIS.md` and `docs/` travel with git. The memory notes are also
   in `jarvis-memory/` (committed) and were copied to `~/.claude/projects/-home-<user>-projects-JARVIS/memory/`.
9. **Check:** `python -m jarvis selftest keyboard` (Windows only; it skips elsewhere) and `python -m jarvis cluster status`.
