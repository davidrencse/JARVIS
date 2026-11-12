# MAIN production bootstrap: implementation blockers

2026-11-12. This change does **not** restore production MAIN switching. No live
systems were contacted, no service restarted, and no operator config changed.

Implemented: the bound MAIN coordinator refuses legacy cutover before candidate
readiness, with instructions for acknowledgement, mode, and lease. The operator
CLI now supports `ack --next-version` while QUIESCED or ROLLBACK_QUIESCED. This is
necessary because `AuthorityStore.set_mode` requires acknowledgements of the
*destination* version when leaving a quiesced mode; ordinary `ack` preserves its
current-version behavior.

The following must be implemented before automatically binding production:

- `configure_production` persists a managed marker. That immediately fences hub
  dispatch and local effects. A runtime alone, without a working admission issuer
  and executor binding, disables those effects. Loading an operator JSON is not
  sufficient evidence that this cutover is safe.
- `cluster main` runs in a separate process. The production binding is an
  in-process dictionary, so hub startup alone cannot make the CLI see it. The
  command needs an authenticated path to the hub's coordinator.
- SafeHandoff needs source drain/release and candidate lease acceptance in their
  actual runtimes, plus authenticated key/endpoint repointing and a canary. There
  is no production transport connecting those callbacks to the nodes. They must
  not be replaced with unconditional success callbacks or local stand-in runtimes.
- Gateway readiness must be signed by the candidate installation. The Zenbook's
  operator client cannot post readiness for the Dell. Owner transition proof
  does not bypass this check.
- `brainsync._peer_brain_command` explicitly refuses Windows targets. The default
  real storage hook therefore cannot complete the requested Dell handoff.
- The operator CLI has no manual recover/lease command. A ready lease requires a
  recover transition with verified readiness, fencing, grant, and canary; setting
  mode V2_ACTIVE is correctly prohibited.

## Operator sequence once the adapters are implemented

Do not perform cutover just to test this partial change. These commands describe
protocol order; they do not supply the missing production adapters.

On the owner PC, using its operator config, inspect status and use the returned
mode_version for each mode command (never assume fixed versions):

```text
jarvis continuity --config ~/.config/jarvis-continuity/operator.json status
jarvis continuity --config ~/.config/jarvis-continuity/operator.json mode PREPARING_V2 --expect-version <current-version>
jarvis continuity --config ~/.config/jarvis-continuity/operator.json status
jarvis continuity --config ~/.config/jarvis-continuity/operator.json mode QUIESCED --expect-version <current-version>
```

On **every eligible participant**, using that participant's own installation key
and operator config, acknowledge fencing for the upcoming destination version:

First stop legacy admission and effects on those PCs. An acknowledgement asserts
that fencing has happened; this CLI command does not itself drain a hub or stop
legacy writers. That local fencing is part of the missing production integration.

```text
jarvis continuity --config <that-PCs-operator.json> ack --next-version
```

Back on the owner PC:

```text
jarvis continuity --config ~/.config/jarvis-continuity/operator.json status
jarvis continuity --config ~/.config/jarvis-continuity/operator.json mode V2_ONLY_NO_MAIN --expect-version <current-version>
```

Then acknowledge the current epoch/version on the participating PCs as required
by the manual recover transition. Obtain the initial ready MAIN lease through
that transition. **There is no supported CLI command for this step yet.** Only
after that and production adapter implementation can the final command be:

```text
jarvis cluster main dell
```

No automatic recovery or promotion is introduced by this change.
