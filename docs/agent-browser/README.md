# agent-browser from source

JARVIS's agent browser is Vercel's open-source [agent-browser](https://github.com/vercel-labs/agent-browser)
(Apache-2.0), pinned at the version in `jarvis/agent_browser.py` (`VERSION`, currently 0.39.1).
`agent_browser.source_build()` uses a source build when one reports that version; otherwise
JARVIS falls back to the npm package through `npx` (the Dell does this).

Build (Linux, no sudo):

    git clone --depth 1 --branch v0.39.1 https://github.com/vercel-labs/agent-browser.git ~/projects/agent-browser
    cd ~/projects/agent-browser
    git apply ~/projects/JARVIS/docs/agent-browser/0001-history-step-via-devtools.patch
    cargo build --release --locked --manifest-path cli/Cargo.toml   # rustup: minimal profile is enough

`AGENT_BROWSER_SOURCE` points JARVIS at a clone somewhere else. After a rebuild, JARVIS stops
any agent-browser daemon older than the binary on its next call (same version, old code).

## 0001-history-step-via-devtools.patch

`back`/`forward` ran `history.back()` inside the page; the navigation killed that evaluation,
so a back that worked was reported as "CDP error (Runtime.evaluate): Inspected target
navigated or closed" (seen 2026-11-01). The patch steps through the tab's history with
`Page.getNavigationHistory` + `Page.navigateToHistoryEntry`, and says "There is no earlier
page to go back to" / "no later page" at either end (the blank page a tab starts on doesn't
count). Not upstreamed yet.
