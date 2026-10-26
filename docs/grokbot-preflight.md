# Grokbot handoff preflight

Automatic web and account matches are candidates for Grokbot, not permission to type the raw request into its window. Before an automatic handoff, an action-brain tool call, a CLI `grokbot task`, or a Devices-view Grok command, JARVIS makes a read-only, tool-free ChatGPT inference. It can return a direct answer, ask for missing context, or prepare one Grok task. A forward decision receives a **second, separate inference** that reviews the task against the original request. If either pass is unavailable, malformed, or fails a check, nothing is sent to Grokbot.

A forwarded task requires all ten named checks:

1. `grok_needed`: Grok's browser or an explicit Grok request is relevant.
2. `goal_clear`: the requested outcome is clear.
3. `scope_bounded`: the task has a bounded scope.
4. `context_sufficient`: enough context is present to begin.
5. `one_handoff`: one Grok handoff can reasonably handle it.
6. `task_faithful`: the prepared task matches the owner's request.
7. `target_clear`: the account, site, person, or subject is clear where needed.
8. `output_clear`: the expected answer or result is clear.
9. `constraints_preserved`: the owner's constraints survive the rewrite.
10. `no_invented_steps`: the task adds no unrelated work.

Code validates the JSON decision, exact check set, boolean values, task length, and control characters. The ten semantic judgments come from the second inference, so they are a quality gate rather than proof of correctness. JARVIS uses separate model processes because the action-brain tool can be called while its own process is busy. Grokbot's existing instruction to ask David before buying, sending, posting, or submitting remains in the handoff prompt; this preflight adds no new approval flow.

The low-level `Grokbot.send` and `handoff` methods are transport primitives. User-facing routes call the preflight before reaching them. The inference can add startup latency and consumes the configured ChatGPT subscription allowance. A model or sign-in failure stops the handoff rather than forwarding the unchecked text.
