# Browser and Grokbot

*The agent-browser build, and the ten checks before a Grokbot handoff.*

## agent-browser

JARVIS uses Vercel's [agent-browser](https://github.com/vercel-labs/agent-browser) (Apache-2.0), pinned at `VERSION` in `jarvis/agent_browser.py` (0.39.1). `agent_browser.source_build()` uses a source build that reports that version. Otherwise JARVIS uses the npm package through `npx`, which is what the Dell does.

Build on Linux, without sudo:

```text
git clone --depth 1 --branch v0.39.1 https://github.com/vercel-labs/agent-browser.git ~/projects/agent-browser
cd ~/projects/agent-browser
git apply ~/projects/JARVIS/docs/agent-browser/0001-history-step-via-devtools.patch
cargo build --release --locked --manifest-path cli/Cargo.toml
```

A minimal rustup profile is enough. `AGENT_BROWSER_SOURCE` points at a clone somewhere else. After a rebuild, the next call stops any agent-browser daemon older than the binary, even when the version string matches.

`0001-history-step-via-devtools.patch` is not upstream. `back` and `forward` used to run `history.back()` inside the page. The navigation killed that evaluation and a successful back was reported as `CDP error (Runtime.evaluate): Inspected target navigated or closed`. The patch steps with `Page.getNavigationHistory` and `Page.navigateToHistoryEntry`, and says there is no earlier or later page at either end. The blank page a tab starts on does not count.

`actions.browser_engines` selects Playwright MCP (`browser_*`, profile `.jarvis/browser-profile`) and agent-browser (`agent_browser_*`, session `jarvis`, profile `.jarvis/agent-browser-profile`). `agent_browser_read` fetches text without opening Chrome. Page scripts, uploads, raw CLI flags, other sessions, and screenshot files on disk are refused. `approve_browser` allows Playwright only and http(s) navigation only. On top of that, `browser_gate.py` parks a `browser_action` confirmation for a click or key that looks like send, post, submit, buy, or delete, for Enter after typing outside a search box, for a typed field with `submit`, and for accepting a page dialog. It waits up to 180 s. The check is a keyword heuristic over the tool arguments. It narrows prompt injection and does not remove it. Page content is untrusted. JARVIS never types passwords. The model is told not to buy, send, post, delete, or submit a personal or payment form until David confirms that exact action.

Saying "in the agent browser" turns open and scroll into the direct `browse` intent. Without that phrase, scrolling means David's own browser window. `frame()` does not launch Chrome; it checks the session's Chrome process first. The console's Agent screen box polls `GET /api/agent-browser` and posts back, reload, stop, and close to `POST /api/agent-browser`.

## Grokbot preflight

An automatic web or account match is a candidate, not permission to type the raw request into Grokbot. Before an automatic handoff, an action-brain tool call, a CLI `grokbot task`, or a Devices-view Grok command, JARVIS runs a read-only, tool-free ChatGPT inference. It may answer, ask for context, or prepare one Grok task. A forward decision gets a second, separate inference that reviews the task against the original request. If either pass is unavailable, malformed, or fails a check, nothing is sent.

A forwarded task needs all ten checks:

1. `grok_needed`
2. `goal_clear`
3. `scope_bounded`
4. `context_sufficient`
5. `one_handoff`
6. `task_faithful`
7. `target_clear`
8. `output_clear`
9. `constraints_preserved`
10. `no_invented_steps`

Code validates the JSON decision, the exact check set, boolean values, task length, and control characters. The ten judgments come from the second inference. They are a quality gate, not proof the task is right. The inferences use separate model processes because the action-brain tool can be called while its own process is busy. The handoff prompt still tells Grokbot to ask David before buying, sending, posting, or submitting. This preflight adds no approval flow.

`Grokbot.send` and `handoff` are transport. User-facing routes call the preflight first. The inference adds startup latency and uses the ChatGPT subscription allowance. A model or sign-in failure stops the handoff. `routing.agent_fallback: false`, or Grokbot not configured, turns the route off.

Grok is outside the trust boundary: one task at a time per device, results stay `agent_claimed` until observed or confirmed. See [jarvis-os-architecture.md](../jarvis-os-architecture.md).
