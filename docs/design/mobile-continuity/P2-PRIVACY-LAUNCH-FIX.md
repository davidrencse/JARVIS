# P2 prep: node console privacy and knowledge launch fix

Closes P1-BOUNDARIES §4 gaps 1 and 3 (knowledge part) in existing code. **Not** identity, leases,
fencing, effect admission or any authority protocol; none of that is added or implied.

## Gap 1: node console reads exposed other requests' content

`cluster.node_api` (the gate in front of the main's console API for a `console`-capable node) let
a node read `/api/history`, `/api/history/commands`, `/api/events`, `/api/tasks/<id>`,
`/api/plans/<id>`, `/api/jobs/<id>`, `/api/cases`, plus full `/api/overview` and `/api/confirm`.
Route bodies inspected (`ui.py`): every one of those carries text a node never sent.

| Route | Body holds | Now (node) |
| --- | --- | --- |
| `/api/history`, `/api/history/commands` | requests and replies from every channel | denied |
| `/api/events` | live bus events (turn text, tasks) | denied |
| `/api/tasks/<id>`, `/api/plans/<id>` | task evidence (recalled memory), plan goals and answers | denied |
| `/api/cases` | investigation titles from memory | denied |
| `/api/jobs/<id>` | job text and result | allowed only if `job.origin_device == node` (stamped by `Console.submit`); main, plan-run, other-node and unknown jobs are 403 |
| `/api/overview` | memory excerpts, task/plan text, config (agents, network, desktop) | top-level allowlist: `version now cluster devices routing models brain live voice tools` |
| `/api/confirm` (GET) | pending (already scoped to the caller's origin), site grants, *recent outcomes of every origin* | allowlist `pending grants` (`recent` dropped) |
| `/api/route /tree /specialists /graph/system /usage /credits /voice /agent /grokbot` | metadata (repo layout, quota, status); `/api/route` echoes the caller's own text | unchanged |

Mechanism (`jarvis/cluster.py`): `NODE_API_READ` / `NODE_API_READ_UNDER` shrunk; `NODE_API_FIELDS`
+ `node_fields()` project the two mixed routes (applied in `MainHandler._send`, GET only, reset per
request so a keep-alive connection cannot carry a projection into `/v1/*` or a POST reply);
`node_job()` scopes job polls. The owner token skips all of it. Authorization and the handler read
the same `urlparse(self.path).path`, so query strings and `;params` cannot change which route is
checked; trailing slash, case and `..` spellings fall through to denial.

Boundaries / honest limits:
- Allowlist projection is by top-level key. The kept keys were read for content: `devices`,
  `cluster.nodes` and `voice` are hardware/status metadata; none embeds request text. A new field
  added to those objects later is exposed to nodes: review when `Console.overview` changes.
- A node's own job reply is returned to it (it asked); jobs it did not start are not.
- `grants` (which sites David allowed) stays visible to a node: it is needed for the revoke UI.
- Oracle never received these; this closes node->PC exposure only. A node holding the `ask`
  capability can still ask the main questions (that is its function).

**Compatibility (honest):** a node's own console loses live updates and history. `/api/events` is
403, so `app.js`'s event stream will fail and retry on a node (not changed here); the history,
trace-replay and investigations panes show nothing; the overview omits memory, tasks, plans and
todo. Owner/main console is unaffected. Asking from a node and polling its own job still work;
confirm approve/reject POSTs still work. A node-safe live feed (events filtered by origin) is a
later task, not built.

## Gap 2: `knowledge._llm_answer` launch

Previously: `claude -p --model <m>` with `subscription_env()` only. User-level settings
(`apiKeyHelper`, env) and session files, built-in tools and plugin MCP/skills were all in play.
Now `CLAIMS_ONLY_FLAGS` is appended: `--setting-sources project --no-session-persistence --tools ""
--strict-mcp-config --disable-slash-commands`, the same set `runner.py` and `fastlane.LEAN_FLAGS`
use, minus tools (this call only rephrases claims given in the prompt). Every flag was confirmed
against the installed CLI's `claude --help` (2.1.322); no inference was run. Unchanged: 120 s
timeout, claim-id citation check, extractive fallback on any empty/uncited/failed result.

Boundaries:
- `--setting-sources project` is read from the process working directory; a project
  `.claude/settings.json` there could still carry an auth helper. This removes the *user*-level
  source (the documented gap); it does not prove credentials cannot arrive, and the fast lane and
  runner share that limit.
- A CLI too old to know a flag exits non-zero with empty stdout, so the answer falls back to the
  extractive text (tested with a fake). The return code is not inspected beyond that.
- Not covered: the other spawn sites in P1-BOUNDARIES §3 (`claude_auth.py`, the brain, FastLane's
  Read/WebFetch talk lane), console-path node refusal for `knowledge`, `brain-budget.json` locking.

## Tests

- New `tests/cluster/test_node_console_privacy.py` (11): unit gates, projections, job scoping and a
  real loopback hub + real `Console` seeded with sentinel private text: every spelling denied for a
  node, node overview/confirm carry no sentinel, own job readable, other jobs denied, owner reads
  everything, `/v1/status` unaffected, no projection leak across a keep-alive connection.
  Mutation check: re-adding `/api/history` to `NODE_API_READ` fails 4 assertions.
- New `tests/intelligence/test_knowledge_launch_policy.py` (7): stubbed `subprocess.run`; argv flags
  and values, model setting, scrubbed env (API-key, Bedrock, parent-session variables absent), citation
  rule, OSError/timeout/unknown-flag/API-error fall back to extractive via `ask(llm="claude")`,
  no launch without the binary or with `llm="none"`.
- Run by this change: `tests.cluster` (287 OK), `tests.data.test_knowledge`,
  `tests.runtime.test_harness_boundaries` (48 together, OK; its node-read pin was already updated
  to denial by the other lead). Full suite not run (parent integrates).

Restart: Python changes, so the main's JARVIS process must restart; nodes only need the main's
code if they import `cluster.py` (they do: run `jarvis cluster update <node>`).
