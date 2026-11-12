# Brain transport

WS3 transport for the vault and for `memory.sqlite3` when MAIN moves. Off until
`brainsync.enabled` is the boolean `true`. The string `"true"` does not enable it.
Nothing in `jarvis.config.json` is changed by this work.

WS1 calls `brainsync.handoff(old_main, new_main, config, base)` only after the old
MAIN is quiesced and fenced, and it refuses activation unless the returned dict has
`ok` true. `transport=` and `between_reads=` are keyword-only. Failures are
`ok: false` with `status` and `error`. They are not exceptions.

`status(config, base)` is read-only. `sync(config, base)` runs only on the current
MAIN. CLI: `jarvis brain sync` and `jarvis brain status`. Hidden `export`,
`receive`, and `freeze` exist for the handoff adapter. They are omitted from
`--help`. `--state` must be `<config parent>/.jarvis`.

## Config

| Key | Default | Meaning |
| --- | --- | --- |
| `brainsync.enabled` | false | Only boolean `true` turns transport on. |
| `brainsync.repo` | `<base>/.jarvis/brain-git` | Dedicated git directory. |
| `brainsync.remote` | unset | Optional git URL. Omitted from `status`. `user:password@` URLs are refused. |
| `brainsync.remote_allowlist` | `[]` | Exact transport paths that may leave the machine. |

Allowlist entries are transport-relative: `vault/<rel>` or `state/memory.sqlite3`.
`.git`, `.obsidian`, and `.jarvis` cannot be allowlisted. An empty allowlist means
the remote receives vault notes only. `state/memory.sqlite3` is never on the remote
unless that exact path is listed.

The repository must not be the JARVIS checkout, `base/.git`, the vault, a directory
that contains the vault, or a linked worktree of the checkout. The default
`<base>/.jarvis/brain-git` is the intended location.

## Architecture

Git is `subprocess` with an argument list, `shell` false, and a minimal environment.
There is no force-push, no autostash, no destructive reset, and no refspec that
starts with `+`. A non-fast-forward push is reported as preserved. Both histories
stay.

Each device publishes `refs/heads/brainsync/<device>`. The private ref
`refs/heads/brainsync-local/<device>` also stores the memory snapshot and is never
pushed. Sync fetches the `brainsync/*` namespace first, materializes it, then
commits, then materializes again.

Materialize is exclusive-create. A vault file is never overwritten or deleted. The
same path with different bytes becomes `conflict-<sha256><suffix>` beside the file.
The snapshot is additive: a note removed from the vault stays in history, and a
later sync can recreate that file from the retained blob. That is intentional. It
is not a user-facing trash.

History that contains a disallowed blob is not pushed. A new orphan
`refs/heads/brainsync/<device>-sanitized` keeps the newest safe blobs and is the
only ref offered to the remote. The dirty ref stays local. Objects already in a
private object store, or already published on a remote, are not deleted.

Memory for handoff is `sqlite3` `backup()` from a read-only connection, never a raw
copy of the database file. The bundle is `JARVIS_BRAIN1`, a JSON header, a git
bundle, and the memory bytes. Caps: 16 MiB per file, 512 MiB per bundle, 100000
files. Export and freeze request bodies are capped at 4096 bytes.

Handoff identity:

- The old MAIN builds the bundle, pushes it, and writes
  `<base>/.jarvis/brainsync-handoff.json` (mode 0444) only after the peer accepts.
  Later `sync` and `export` on that device return `handoff_readonly`. A matching
  retry resends the stored bundle and does not rebuild it.
- The new MAIN pulls the export, imports the bundle, then asks the old MAIN to
  freeze. Memory is installed only when the source snapshot is unchanged across two
  backups. An existing different nonempty destination database is left in place and
  the incoming snapshot is written beside it as `memory.sqlite3.conflict-<sha256>`.
  The call returns `memory_conflict`. An empty destination is copied aside as
  `memory.sqlite3.prior-<hash>` and then replaced. Identical bytes are a no-op.
- Any other device returns `not_participant`. `old_main` and `new_main` must be keys
  of `config["devices"]`.

Quiesce is two identical backup snapshots on the source. `between_reads` is honored
only on the push path. A pull refuses a local `between_reads` hook
(`source_not_quiesced`) because the hook cannot see the source database. Two
matching snapshots are necessary evidence that this process did not observe a
write. They are not a proof that WS1's fence held. The marker binds later
brainsync calls only. Other writers remain WS1's job.

Offline or unreachable source: `ok` false, `status` and `error` `source_offline`.
The destination is unchanged. The result does not use the word `latest`. Success
status is `handed_off`.

## Production adapter

`SshBundleTransport` is the default. It calls `deploy._remote` with one of these
fixed command strings and a stdin payload:

- `python -m jarvis brain receive` — raw bundle on stdin
- `python -m jarvis brain export` — JSON request on stdin, base64 bundle on stdout
- `python -m jarvis brain freeze` — JSON request on stdin

`deploy._remote` decodes stdout as UTF-8 with replacement, so a git bundle cannot
ride stdout. Export is base64. Receive uses stdin bytes. Device ids, paths, and
shell syntax are not interpolated. A refspec, `--force`, or a `+` prefix is not
sent.

Pull-mode peers (`transport` other than `ssh`) cannot be reached. `_remote` raises,
and handoff returns `source_offline`.

Transfer jobs are not the receiver. `transfer.py` writes `~/JARVIS-Drop`. Changing
that receiver would edit files this workstream does not own.

### Peer command selection

When the peer device in `config["devices"]` has a POSIX absolute `jarvis_dir`, the
adapter runs `<jarvis_dir>/.venv/bin/python -m jarvis --config <jarvis_dir>/jarvis.config.json
--state <jarvis_dir>/.jarvis brain <receive|export|freeze>`, each path `shlex.quote`d, so the
peer checkout is used regardless of login `PATH` or cwd. A present but unsafe `jarvis_dir`
(relative, `..`, NUL/newline) or a Windows `os`/`platform`/`system` hint fails closed with
`peer_path_unsafe`. **Limit:** a peer with no `jarvis_dir` still gets bare
`python -m jarvis brain ...` from its login shell, so set `jarvis_dir` on every peer before
enabling brainsync. Windows peers are refused by this adapter.

### Fail-closed rules (added in the wrap-up round)

- Export refuses (`omitted_notes`) when any selected vault note would be left out
  (too large, symlink, unreadable, bad path, secret). No bundle or pending file is written.
- Import refuses (`materialize_incomplete`) when any bundle path could not be written
  (symlink or unsafe destination). Memory is not installed and the source is not frozen.
- Freeze refuses (`source_changed`) when the source memory or vault differs from what the
  pending bundle captured. No marker is written.

No live SSH, Dell, GitHub, or Windows handoff has been run. The tests use a local
bare repository, two clones, and an in-process stand-in for `_remote` that still
enters `SshBundleTransport` and the real bundle import/export.

## Decision

Which private GitHub remote should brain sync use, and should `memory.sqlite3` be
in it?

Recommendation: one dedicated private repository whose only content is the vault
snapshot (`refs/heads/brainsync/<device>`). Do not use the JARVIS source
repository. Do not put `memory.sqlite3` on GitHub. Memory moves only inside the
direct bundle during handoff, and only when the source snapshots match. Leave
`brainsync.enabled` false until that remote exists and the peer `python -m jarvis`
check below succeeds. No new dependencies.

## What David runs

Placeholders only. Do not run these against the live vault until the private remote
exists and `brainsync.enabled` is set on purpose.

```fish
cd /path/to/JARVIS
env JARVIS_NO_MODELS=1 .venv/bin/python -m jarvis --config /path/to/JARVIS/jarvis.config.json --state /path/to/JARVIS/.jarvis brain status
env JARVIS_NO_MODELS=1 .venv/bin/python -m jarvis --config /path/to/JARVIS/jarvis.config.json --state /path/to/JARVIS/.jarvis brain sync
```

Peer PATH check, not executed:

```fish
# ssh <user>@<peer> python -m jarvis brain status
```

Focused tests, from the checkout. The inner command is the charter invocation;
`bash -c` keeps it one fish line:

```fish
bash -c 'cd /path/to/JARVIS && env $(env | grep -o "^CODEX_[A-Z_]*" | sed "s/^/-u /") JARVIS_NO_MODELS=1 .venv/bin/python -m unittest tests.data.test_brainsync'
```

## Limits

- Linux is what the focused suite ran on. Windows paths and reparse points are
  written for, and were not executed.
- A contaminated blob can remain in the source object store. An already-published
  remote ref is not rewritten or deleted.
- Sync can recreate a vault file the user deleted, because retained history is
  additive.
- An empty destination memory database can be replaced after a side copy. A
  conflicting nonempty database is kept and the incoming snapshot is stored beside
  it. Neither database is deleted.
- The handoff marker does not fence vault writers, the memory writer, or cluster
  role changes. WS1 does that before it calls `handoff`.
- GitHub repo creation, enabling the flag, and a real cluster handoff are not part
  of this change.
