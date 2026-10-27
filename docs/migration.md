# Moving JARVIS's main to another machine (e.g. the Arch Zenbook)

1. **On the current main (Dell): commit everything.** `git status` must be clean, then push.
   `.jarvis/` is not in git — copy it separately (step 4).
2. **On the new machine: get a clean checkout.** The Zenbook's `~/projects/JARVIS` was
   tar-synced by the cluster deployer, so it isn't a clean git tree. Either
   `mv ~/projects/JARVIS ~/projects/JARVIS.old && git clone <remote> ~/projects/JARVIS`, or
   inside it `git fetch && git reset --hard origin/main` (a backup `.tgz` already exists).
3. **Python deps:** create a venv and `pip install -e .` (pyproject.toml); the MiniLM ONNX model downloads on first semantic use.
4. **Copy state for continuity** (from the Dell, over SSH):
   `scp -r C:/Users/jdoe/Desktop/JARVIS/.jarvis/{memory.sqlite3,history.sqlite3} zenbook:~/projects/JARVIS/.jarvis/`
   Stop the JARVIS daemon on both sides first so the SQLite files aren't mid-write.
5. **Make the Zenbook main:** `python -m jarvis cluster configure --main zenbook`, and set
   `"cluster": {"tunnel": "forward"}` so the Dell (node) dials the Zenbook's sshd — Windows needs
   no SSH server then. Give `devices.zenbook` `transport: ssh` and a `target` the Dell can reach.
6. **Autostart:** `python -m jarvis cluster autostart on` on each machine (systemd user unit on
   Arch, Startup .vbs on Windows). Then `cluster update dell` to push matching code to the node.
7. **Vault:** `vault.path.linux` in config points at `~/Documents/obsidian-vault`; make sure it syncs there.
8. **Claude context:** this repo's `CLAUDE.md`, `JARVIS.md` and `docs/` travel with git. Claude's
   per-machine memory notes don't — copy them to `~/.claude/projects/-home-<user>-projects-JARVIS/memory/`.
9. **Check:** `python -m jarvis selftest` and `python -m jarvis cluster status`.
