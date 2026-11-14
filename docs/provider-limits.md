# Provider quotas

*How JARVIS reads Claude and Codex plan windows. Point-in-time percentages are not recorded here; they expire with the window.*

Subscription percentages are the plan allowance across that product's surfaces. A local token tally is not the remaining allowance. Context length is a third limit. Dollar estimates are not the bill. Usage credits, subscription windows, and API billing stay separate.

Claude's five-hour and weekly windows are described in [Claude's usage help](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work). The person-facing checks are Claude Code `/usage` and [Settings → Usage](https://claude.ai/settings/usage). Codex documents `account/rateLimits/read` on the app-server, including percents, window duration, and reset timestamps ([Codex app-server](https://learn.chatgpt.com/docs/app-server)). No fixed token or message ceiling is inferred from those responses.

## What JARVIS calls

`jarvis/credits.py` reads the live quota before local logs.

- Claude: authenticated GET `https://api.anthropic.com/api/oauth/usage`, using the Claude Code sign-in. The header shape was taken from the installed CLI.
- Codex: authenticated GET `https://chatgpt.com/backend-api/wham/usage`, using the Codex ChatGPT sign-in.
- Both URLs are internal compatibility surfaces, not a stable public API. The call does not infer, log in, buy, or change configuration. If the endpoint is down, the local or manual fallback stays available.
- Requests use a fixed HTTPS destination, refuse redirects, time out at 8 seconds, and bound the body. Errors are sanitized. One lock is shared. Success and failure are cached for 60 seconds. The panel cache stays 120 seconds. A forced refresh still waits out the shorter cache.
- Unknown window names are not given a guessed meaning. Extra-usage spending is not shown as a prepaid balance.
- An expired Codex log window is not reported as 100% available. The old percent is kept as history and current usage is unknown.
- The Claude hourly chart reads local logs only. It does not request quota. Removed cache rows, future-dated rows, and malformed numbers are dropped.
- `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honored. The usual Windows CLI credential files are what this reads. An OS keychain is not extracted; that account falls back to local data and the provider's own dashboard.

The dashboard contract is unchanged: live source, numeric percent meters, reset timestamps, plan facts, and a remaining-percent headline.

## Claude session renewal

Access tokens expire. JARVIS does not stop at "sign-in expired".

- When the saved token is expired, or the usage endpoint returns 401 for a token that still looks unexpired, `jarvis/claude_auth.py` starts `claude` once with only an SDK `initialize`, then closes stdin. Claude Code renews its own session. JARVIS sends no prompt, starts no model turn, runs no login, and never writes credentials.
- The child uses `--safe-mode`, no tools, no MCP, no Chrome, no session persistence, an empty temporary directory, and a 20-second timeout. API keys, base URLs, and Bedrock or Vertex switches are removed so only the existing subscription sign-in is used.
- After the process exits, JARVIS rereads the credentials and requires a new unexpired access token. A clean exit is not enough. A 401 gets one renewal. A token that did not change is a rejected sign-in, not a loop.
- Concurrent checks share one CLI start. Each config directory has a 60-second cooldown, so a revoked sign-in cannot spawn a process on every refresh.
- Renewal failure (timeout, missing CLI, revoked sign-in, missing refresh token) is shown in Credits as "run claude auth login". The local tally stays visible.
- The quota cache drops when the credentials file changes, or when a cached window's reset time passes. The panel shows when the provider was fetched, not when the cache was read.
- If the CLI rewrites the credentials file and leaves them unusable, that rewrite is the CLI's, not a concurrent sign-in. It must still surface "run claude auth login".

A revoked account, or one whose refresh token has expired, needs `claude auth login`. JARVIS cannot sign in. `claude auth status` can say logged in without restoring quota access.

```text
python -m unittest discover -s tests -p test_provider_limits.py -v
python -m unittest tests.runtime.test_quota_renewal
python -m unittest tests.security.test_claude_auth
```
