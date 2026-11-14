# Moving JARVIS's main

*How to make another machine the main. The configured main is `zenbook`.*

`.jarvis/` is not in git. Copy it separately. Snapshot `memory.sqlite3` and `history.sqlite3` with SQLite's backup API, which is safe while JARVIS is running, or stop the daemons first. On the new machine, keep the previous files as `*.pre-main` and move the snapshots into `.jarvis/` only after JARVIS there is stopped.

1. On the current main, leave the git tree clean and push. `CLAUDE.md`, `JARVIS.md`, `docs/`, and `jarvis-memory/` travel with the clone. Claude memory notes under `~/.claude/projects/` do not.
2. On the new machine, use a real git checkout. A tree that was only tar-synced by the deployer is not one. Either move it aside and `git clone`, or `git fetch` and `git reset --hard origin/main` after a backup.
3. Create a venv and `pip install -e .`. JARVIS does not download models at run time. Fetch MiniLM once, or semantic recall and the decider stay off:

```text
python -c "from huggingface_hub import hf_hub_download as d; M='sentence-transformers/all-MiniLM-L6-v2'; d(M,'onnx/model.onnx'); d(M,'tokenizer.json')"
```

4. On the new main: `python -m jarvis cluster init`, then `python -m jarvis cluster invite <old-main>`. `init` says so if sshd is off. Forward tunnel is what `init` sets: only the main needs an SSH server.
5. On the old main, in its checkout: `python -m jarvis cluster join "<code>"`. The node pulls the main's code itself and installs the tunnel and agent at login. `python -m jarvis cluster doctor` on either side lists what is left. After a move, put the same `jarvis.config.json` on every machine. Old node links point at the old main, so invite them again.
6. Vault: `vault.enabled` and `vault.recall` are on, `vault.write` is off. `vault.path.linux` is `~/Documents/obsidian-vault`. If write is turned on, that directory has to be the synced vault.
7. Check: `python -m jarvis cluster status`, and `python -m jarvis selftest keyboard` on Windows (it skips elsewhere).

Machine inventory and live SSH targets are in [handbook/operations.md](handbook/operations.md). Code rollout after the move is [handbook/release.md](handbook/release.md).
